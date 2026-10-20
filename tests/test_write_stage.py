# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The write stage of a run, see process.write() and docs/design.md, "Write stage". Every check and decision runs on
the file as found, then the run writes it once. A planned remux carries the flag edits, else mkvpropedit makes them,
with two guards: a file with bytes past its Segment end takes the edits in a remux, and a layout that the edit broke
gets its repair in the same run. The flow tests fake the tools, see the env fixture. The files of the other tests are
made up."""
import copy
import fcntl
import json
import os
import signal
import subprocess
import types

import pytest

from test_arr_media_guard import (HEADER_HP, REAL_FLOCK, REF, SHAPES, analyzes, burn, decided, english_film, enqueue, env, garbled_av,  # noqa: F401
                                  garbled_run, hearing, hook, japanese_burn, last_decided, layouts, log_lines, mk, queue_analysis, sub_probe,
                                  sub_time_film, talk, verdict)

D = hook.arr_decide
TRACKS_LATE = "Tracks sits after the first Cluster, and no SeekHead before it lists it"
LAYOUT = f"the header layout breaks the Matroska rules: {TRACKS_LATE}"
LAYOUT_HP = dict(HEADER_HP, issue=[f"{LAYOUT}"], first=False, edit_tail=0, edit_blocked=[], enclosed=None, tail=0)
JOINED = "the bytes past the Segment end start another Matroska file"


def header_issue(env, monkeypatch, result=("header_repaired", "header repaired"), after=None, repairable=True, on_edit=(), **hp):
    """A file whose header has the layout issue LAYOUT_HP, with the fields hp, until a remux ran. The probe with edit
    gives edit_blocked on_edit for edit_blocked None. The video check finds damage unless repairable, and records the issue
    it got. repack() records (apply, its edits) for each call. An applied one that works writes the new file under a
    new inode, as Swap's rename leaves it: one more byte, and the probe after, else the file's probe with the edits
    applied. result is what an applied repack() gives. Returns the calls."""
    calls = {"probes": 0, "checks": [], "repairs": [], "done": False, "edit": []}
    issue = dict(LAYOUT_HP, **hp)
    for i, t in enumerate(env["probe"]["tracks"]):   # mkvmerge -J numbers the tracks, and a remux maps the edits by them
        t["id"] = i

    def header_of(path, j=None, edit=False):
        calls["probes"] += 1
        calls["edit"].append(edit)
        return None if calls["done"] else dict(copy.deepcopy(issue), **({"edit_blocked": issue["edit_blocked"] or list(on_edit)} if edit else {}))

    def video_check(path, again=False, j=None, hp=None):
        calls["checks"].append(hp and hp["issue"])
        h = dict(hp, repairable=repairable, blocked=[] if repairable else ["a blank gap"]) if hp and hp["issue"] else hp
        return None, [], {"fault": None, "header": h, "zeros": {"hits": []}}

    def repack(path, j, st, apply, hp=None, edits=()):
        calls["repairs"].append((apply, [list(e) for e in edits]))
        if hook.links(st) > 1:
            return "header_repair_skipped", "header repair skipped, hardlinked: " + "; ".join(hp["issue"]), {}
        if not apply:
            return "would_repair_header", "would repair header: " + "; ".join(hp["issue"]), {}
        if result[0] in hook.REPAIRED:
            calls["done"] = True
            env["files"][path] = copy.deepcopy(after) if after else hook.planned_state(j, edits)[0]
            with open(path + ".new", "wb") as f:   # a new size, as the new file of a remux has
                f.write(open(path, "rb").read() + b"r")
            os.chmod(path + ".new", os.stat(path).st_mode)
            os.replace(path + ".new", path)
        return (*result, {"old_size": st.st_size, "old_duration": hp["duration"], "end": hp["end"]})
    for name, fake in (("header_of", header_of), ("video_check", video_check), ("repack", repack)):
        monkeypatch.setattr(hook, name, fake)
    return calls


def rescans(env):
    return [w for w in env["writes"] if w[1] == "command"]


def test_a_layout_issue_and_the_flag_edits_take_one_remux(env, monkeypatch):
    """The import plans the header repair and decides on the file as found. One remux repairs the header and carries
    every flag edit, so mkvpropedit never runs. The record names the edits, and the app gets one rescan."""
    calls = header_issue(env, monkeypatch)
    hook.main([])
    rec = decided(env)
    edits = rec["edits"]
    assert edits and calls["repairs"] == [(True, edits)] and env["mkvpropedit"] == [], calls
    assert (rec["outcome"], rec["reasons"][0], rec["edits"], rec["replan_edits"]) == ("edited", "header_repaired", edits, 0), rec
    assert [r.get("outcome", r["result"]) for r in log_lines(env)][:2] == ["header_repaired", "edited"]   # no "editing" line, as no mkvpropedit ran
    assert D.unapplied(env["files"][env["path"]], edits) == [] and len(rescans(env)) == 1 and rec["header_repair"]["rescan"] == "sent"
    assert analyzes(env) == ["/library/metadata/7101/analyze"]


def test_a_planned_remux_under_the_shared_lock_raises_no_replan(env, monkeypatch):
    """A job process checks under the shared lock and takes it exclusive for the write. A remux no longer runs the job
    again from the start."""
    calls = header_issue(env, monkeypatch)
    ops = []

    def flock(f, op):
        if f.name.endswith("/lock"):
            ops.append({fcntl.LOCK_SH: "shared", fcntl.LOCK_EX: "exclusive", fcntl.LOCK_UN: "unlock"}[op & ~fcntl.LOCK_NB])
        REAL_FLOCK(f, op)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    assert [r for r in log_lines(env) if r.get("result") == "warning"] == [] and len(calls["checks"]) == 1, log_lines(env)
    assert ops[:3] == ["shared", "unlock", "exclusive"] and calls["repairs"] == [(True, decided(env)["edits"])] and env["mkvpropedit"] == []


def test_a_failed_remux_runs_the_fallback_edit(env, monkeypatch):
    """The original stays, and mkvpropedit makes the edits of the plan. The header finding says why the repair failed.
    The run tries no second header repair."""
    calls = header_issue(env, monkeypatch, result=("header_repair_failed", "header repair failed: the size changed from 1000 to 2000 bytes"))
    monkeypatch.setattr(hook, "layout_of", lambda path: TRACKS_LATE)   # the layout stays broken, and no second repair runs
    hook.main([])
    rec = decided(env)
    assert rec["edits"] and calls["repairs"] == [(True, rec["edits"])] and len(env["mkvpropedit"]) == 1 and rec["outcome"] == "edited", rec
    assert rec["reasons"][0] == "header_repair_failed" and rec["findings"] == [{"kind": "header", "why": "the size changed from 1000 to 2000 bytes"}]
    assert rescans(env) == []


def test_a_failed_remux_of_a_file_with_a_tail_keeps_its_flags(env, monkeypatch):
    """mkvpropedit would put the bytes past the Segment end inside the Segment, so the flags stay. One alert, the edit
    finding, says why."""
    calls = header_issue(env, monkeypatch, result=("header_repair_failed", "header repair failed: the size changed"), edit_tail=700)
    hook.main([])
    rec = decided(env)
    assert len(calls["repairs"]) == 1 and env["mkvpropedit"] == [] and rec["outcome"] == "edit_failed", rec
    assert rec["result"] == "not edited, as a flag edit would put the 700 bytes past its end inside the Matroska Segment: " \
                            "header repair failed: the size changed", rec["result"]
    assert [f["kind"] for f in rec["findings"]] == ["edit"] and rec["header_repair"]["edit"] is True


TAIL = "a flag edit would put the 700 bytes past its end inside the Matroska Segment"


def test_flag_edits_of_a_file_with_a_tail_ride_one_remux(env, monkeypatch):
    """A tail with no issue plans no remux. The flag edits then take one header repair in place of mkvpropedit, after
    the probe with edit and the video check that any header repair gets."""
    calls = header_issue(env, monkeypatch, issue=[], edit_tail=700, edit_blocked=None, result=("tail_removed", "tail removed"))
    hook.main([])
    rec = decided(env)
    assert rec["edits"] and calls["repairs"] == [(True, rec["edits"])] and env["mkvpropedit"] == [], calls
    assert calls["edit"] == [False, True] and calls["checks"] == [[], [TAIL]], calls   # the import's video check, then the one of the remux
    assert (rec["outcome"], rec["reasons"][0], rec["header_repair"]["code"]) == ("edited", "tail_removed", "tail_removed"), rec
    assert rec["header_repair"]["issue"] == [TAIL]


def test_damage_keeps_the_flags_of_a_file_with_a_tail(env, monkeypatch):
    """The video check of the remux finds damage, and mkvpropedit would enclose the tail, so the run writes nothing."""
    calls = header_issue(env, monkeypatch, issue=[], edit_tail=700, repairable=False)
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [] and env["mkvpropedit"] == [] and rec["outcome"] == "edit_failed", rec
    assert rec["result"] == f"not edited, as {TAIL}: a blank gap", rec["result"]


def test_an_edit_blocked_tail_writes_nothing(env, monkeypatch):
    """Another Matroska file past the Segment end blocks the remux. The run writes nothing, keeps the flags and alerts."""
    calls = header_issue(env, monkeypatch, issue=[], edit_tail=700, edit_blocked=[JOINED])
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [] and env["mkvpropedit"] == [] and rec["outcome"] == "edit_failed", rec
    assert rec["result"] == f"not edited, as a flag edit would put the 700 bytes past its end inside the Matroska Segment: {JOINED}"
    assert [f["kind"] for f in rec["findings"]] == ["edit"] and "header_repair" not in rec and rec["after"] == rec["before"]
    assert rec["alerts"][0].endswith("The file still opens, and its default tracks are the Portuguese audio (track 1) and the Portuguese subtitles (track 1)."), rec["alerts"]


def test_a_read_only_file_gets_no_edits_in_the_remux(env, monkeypatch):
    """The remux still repairs the header, as before. The edits stay off the file, so its outcome stays read_only."""
    calls = header_issue(env, monkeypatch)
    os.chmod(env["path"], 0o444)
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [(True, [])] and env["mkvpropedit"] == [] and rec["outcome"] == "read_only", rec
    assert rec["reasons"][0] == "header_repaired" and rec["edits"] and rec["replan_edits"] == len(rec["edits"])


def test_a_hardlinked_file_is_neither_remuxed_nor_edited(env, monkeypatch):
    calls = header_issue(env, monkeypatch)
    os.link(env["path"], env["path"] + ".download")
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [(True, decided(env)["edits"])] and env["mkvpropedit"] == [], calls
    assert (rec["outcome"], rec["reasons"][0]) == ("hardlinked", "header_repair_skipped"), rec


def test_a_dry_run_plans_the_remux_and_records_the_edits(env, monkeypatch):
    calls = header_issue(env, monkeypatch)
    rec = hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", 120, mode="backfill", apply=False, post=False))
    assert calls["repairs"] == [(False, [])] and env["mkvpropedit"] == [], calls
    assert (rec["outcome"], rec["reasons"][0]) == ("dry_run", "would_repair_header") and rec["edits"], rec


LAVF = {"container": {"type": "Matroska", "properties": {"duration": 7200 * 10**9, "writing_application": "Lavf61.7.100"}},
        "tracks": [mk("video", "und", 1, True), mk("audio", "por", 2, True, audio_channels=2),
                   mk("subtitles", "eng", 3, False, codec_id="S_TEXT/UTF8"), mk("subtitles", "eng", 4, False, codec_id="S_TEXT/UTF8")]}
for _i, _t in enumerate(LAVF["tracks"]):
    _t["id"] = _i


def test_the_replan_after_a_remux_makes_what_it_still_asks(env, monkeypatch):
    """A Lavf file has no statistics tags, so both English subtitles read as full, and the first turns on. mkvmerge's
    tags then show it sparse, a forced track, so the plan of the new file turns the second one on instead. mkvpropedit
    turns the first off again and the second on in the same run, and the record counts both. The record names the
    change from the original, and the final flags are those of 2.18.3."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Portuguese"}
    env["probe"] = copy.deepcopy(LAVF)
    first = D.decide(env["probe"], "Portuguese")["edits"]
    after = hook.planned_state(env["probe"], first)[0]
    after["container"]["properties"]["writing_application"] = "mkvmerge v102.0"
    for t, n in zip(after["tracks"][2:], ("50", "1500")):   # 0.4 and 12.5 events a minute
        t["properties"]["tag_number_of_frames"] = n
    calls = header_issue(env, monkeypatch, after=after)
    hook.main([])
    rec = decided(env)
    assert first == [["track:=3", 1, 0]] and calls["repairs"] == [(True, first)], calls
    assert env["mkvpropedit"] == [["--edit", "track:=3", "--set", "flag-default=0", "--edit", "track:=4", "--set", "flag-default=1"]]
    assert (rec["outcome"], rec["replan_edits"], rec["edits"]) == ("edited", 2, [["track:=4", 1, 0]]), rec   # the change from the original
    assert [t["default"] for t in rec["before"]] == [True, False, False] and rec["undo"][2:] == ["--edit", "track:=4", "--set", "flag-default=0"], rec
    remuxed = [r for r in log_lines(env) if r.get("outcome") == "header_repaired"][0]   # its line names the undo of the edit it carried
    assert remuxed["undo"][2:] == ["--edit", "track:=3", "--set", "flag-default=0"], remuxed
    final = {t["properties"]["uid"]: t["properties"]["default_track"] for t in env["files"][env["path"]]["tracks"]}
    assert (final[3], final[4]) == (False, True) and rec["tracks"][-1]["role"] == "full", rec["tracks"]


def subtitle_run(env, monkeypatch, subs, cues, result=("subtitles_remuxed", "subtitles remuxed"), **hp):
    """An import of an English film with the subtitles subs, whose words the check hears as cues {place: cues}, and a
    header as header_issue() makes it with hp. resub() records (its fixes, drop, edits, the issue of its hp), and one
    that works writes the new file. Returns (the header calls, the resub calls, the decision line)."""
    english_film(env, *subs)
    hearing(env, monkeypatch, cues)
    calls, got = header_issue(env, monkeypatch, **hp), []

    def resub(path, j, st, apply, fixes, drop=(), ends=None, edits=(), hp=None, **k):
        got.append((sorted(fixes), list(drop), [list(e) for e in edits], hp and hp["issue"]))
        if result[0] == "subtitles_remuxed":
            env["files"][path] = new = hook.planned_state(j, edits)[0]
            new["tracks"] = [t for t in new["tracks"] if t["id"] not in drop]
            new["container"] = dict(new["container"], properties=dict(new["container"]["properties"]))
            new["container"]["properties"]["duration"] += 5 * 10**9   # a header repair takes the new duration, a subtitle remux alone keeps it
            calls["done"] = True
        return (*result, {"warnings": None})
    monkeypatch.setattr(hook, "resub", resub)
    hook.main([])
    return calls, got, decided(env)


def test_a_retime_and_the_flag_edits_take_one_subtitle_remux_with_the_header(env, monkeypatch):
    """A subtitle fix plans the subtitle remux. It repairs the layout issue and carries the flag edit in the same
    remux, so no header repair and no mkvpropedit run, and the app gets one rescan."""
    calls, got, rec = subtitle_run(env, monkeypatch, [("eng", True, {})], {"s1": talk.cues(talk.RIGHT, offset=1.2)})
    assert rec["file_duration"] == round(talk.DURATION) + 5   # the header repair wrote the duration of the streams
    assert got == [([2], [], rec["edits"], [LAYOUT])] and rec["edits"] and calls["repairs"] == [] and env["mkvpropedit"] == [], (got, calls)
    assert rec["reasons"][:2] == ["subtitle_retimed", "header_repaired"] and rec["outcome"] == "edited", rec
    assert rec["header_repair"]["rescan"] == rec["subremux"]["rescan"] == "sent" and len(rescans(env)) == 1


def test_the_remux_carries_no_edit_of_a_track_it_removes(env, monkeypatch, settings):
    """The decision counts the track that does not match as unmatched, so it loses its flags. The remux takes it out,
    so its edit stays out of the remux, and the plan of the new file asks for nothing more."""
    settings(keep_days=7)
    calls, got, rec = subtitle_run(env, monkeypatch, [("eng", True, {}), ("eng", True, {"track_name": "SDH"})],
                                   {"s1": talk.cues(talk.RIGHT), "s2": talk.cues(talk.OTHER)}, issue=[])
    ((fixes, drop, edits, hp),) = got
    assert (drop, edits, hp, env["mkvpropedit"]) == ([3], [["track:=10", 0, 1]], None, []), got
    assert rec["file_duration"] == round(talk.DURATION), rec["file_duration"]
    assert rec["subremux"]["removed"] == ["s2"] and rec["replan_edits"] == 0 and rec["outcome"] == "edited", rec


def test_a_folded_header_repair_that_fails_posts_one_alert_and_edits(env, monkeypatch):
    """The subtitle remux failed, so the original stays, and mkvpropedit makes the edit. The subtitle alert names the
    failure, and no header alert repeats it. The layout the edit leaves broken still gets its repair."""
    monkeypatch.setattr(hook, "layout_of", lambda path: TRACKS_LATE)
    calls, got, rec = subtitle_run(env, monkeypatch, [("eng", True, {})], {"s1": talk.cues(talk.RIGHT, offset=1.2)},
                                   result=("subtitle_remux_failed", "subtitle remux failed: the proof refused it"))
    assert len(got) == 1 and calls["repairs"] == [(True, [])] and len(env["mkvpropedit"]) == 1 and rec["outcome"] == "edited", (got, calls)
    assert rec["header_repair"]["code"] == "header_repaired" and "header" not in [f["kind"] for f in rec["findings"]], rec["findings"]
    assert "evidence" in rec and rec["size"] == os.path.getsize(env["path"]), sorted(rec)   # the run probed the repaired file


def test_a_folded_header_repair_that_fails_posts_no_header_alert(env, monkeypatch):
    calls, got, rec = subtitle_run(env, monkeypatch, [("eng", True, {})], {"s1": talk.cues(talk.RIGHT, offset=1.2)},
                                   result=("subtitle_remux_failed", "subtitle remux failed: the proof refused it"))
    assert (rec["header_repair"]["code"], rec["header_repair"]["remux"], calls["repairs"]) == ("header_repair_failed", "subtitles", []), rec
    assert "header" not in [f["kind"] for f in rec["findings"]] and len(env["mkvpropedit"]) == 1, rec["findings"]


def test_a_failed_remux_keeps_the_flags_of_a_garbled_track_it_was_to_take_out(env, monkeypatch, settings, tmp_path, garbled_av):
    """The remux would take out the garbled Polish track, so the plan gave it no flag. It failed, so the track stays,
    and the fallback edit plans its flags as 2.18.3 did: a Polish film keeps its Polish subtitles on."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Polish"}
    garbled_run(env, monkeypatch, settings, tmp_path, garbled_av, [(10 + 3.0 * i, 12.5 + 3.0 * i, t) for i, t in enumerate(SHAPES["pol_lost"][0]())],
                "cp1250", "pol", film_lang="pol")
    monkeypatch.setattr(hook, "sub_verdicts", lambda *a, **k: {})
    real, seen, decide = os.replace, [], D.decide
    monkeypatch.setattr(os, "replace", lambda a, b, **kw: (_ for _ in ()).throw(OSError(5, "Input/output error"))
                        if str(a).endswith(".repack-tmp") and str(b) == env["path"] else real(a, b, **kw))
    monkeypatch.setattr(D, "decide", lambda *a, **k: seen.append(set(a[7]) if len(a) > 7 else set()) or decide(*a, **k))
    with pytest.raises(SystemExit):
        hook.main(["--sub-time", env["path"], "--apply"])
    rec = last_decided(env)
    assert {"s1"} in seen, seen   # the plan of the remux gave the track it takes out no flag
    assert rec["subremux"]["codes"] == ["subtitle_remux_failed"] and rec["garbled"]["s1"]["repair"] is False, rec["subremux"]
    assert not rec.get("edits") and hook.mkvmerge(env["path"])["tracks"][2]["properties"]["default_track"], rec.get("edits")


def test_edit_blocked_bytes_stop_the_subtitle_remux_too(env, monkeypatch):
    """The subtitle remux would drop another Matroska file past the Segment end, which the probe with edit finds. The
    run writes nothing."""
    calls, got, rec = subtitle_run(env, monkeypatch, [("eng", True, {})], {"s1": talk.cues(talk.RIGHT, offset=1.2)},
                                   issue=[], edit_tail=700, edit_blocked=None, on_edit=[JOINED])
    assert got == [] and calls["repairs"] == [] and env["mkvpropedit"] == [] and rec["outcome"] == "edit_failed", rec
    assert rec["subremux"]["result"] == f"subtitle remux skipped, {JOINED}" and rec["subremux"]["codes"] == ["subtitle_remux_skipped"]


def test_a_remux_waits_for_the_exclusive_lock_with_no_flag_edit(env, monkeypatch):
    """The remux of a job process runs under the exclusive lock, also when the run has no flag edit."""
    calls, state = header_issue(env, monkeypatch), []
    env["probe"] = {"container": {"properties": {"duration": 7200 * 10**9}},   # English plays first, so no flag edit
                    "tracks": [mk("video", "und", 1, True), mk("audio", "eng", 2, True, audio_channels=6), mk("subtitles", "eng", 3, False)]}
    real = hook.repack
    monkeypatch.setattr(hook, "repack", lambda path, j, st, *a, **k: state.append(fcntl_state(path)) or real(path, j, st, *a, **k))
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    assert state == ["exclusive"] and calls["repairs"] == [(True, [])] and "edits" not in decided(env), state


def fcntl_state(path):
    """Whether this process holds the file lock of the state folder exclusive, as a second exclusive try shows."""
    with open(os.path.join(hook.CFG.state_dir, "lock"), "w") as f:
        try:
            REAL_FLOCK(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "exclusive"
        REAL_FLOCK(f, fcntl.LOCK_UN)
        return "shared"


def test_a_second_run_over_an_edited_file_posts_no_problem_twice(env, monkeypatch):
    """The marker of an alert keys on the size of the file at the post, after the edit grew it. A rerun finds the same
    problem at the same size and posts nothing."""
    monkeypatch.setattr(hook, "check_audio", lambda path, j, edits, runtime=0: (None, ["the audio is silent from 0:10 to 0:50"], []))
    real = hook.subprocess.run

    def run(argv, **kw):   # mkvpropedit writes the grown Tracks at the end
        out = real(argv, **kw)
        if argv[0] == "mkvpropedit":
            with open(argv[1], "ab") as f:
                f.write(b"t" * 100)
        return out
    monkeypatch.setattr(hook.subprocess, "run", run)
    posts = lambda: [b for m, u, b in env["http"] if m == "POST" and "discord" in u]
    hook.main([])
    assert len(env["mkvpropedit"]) == 1 and len(posts()) == 1
    hook.main([])
    assert len(env["mkvpropedit"]) == 1 and len(posts()) == 1, posts()


def test_no_rescan_without_a_remux(env, monkeypatch):
    hook.main([])
    assert len(env["mkvpropedit"]) == 1 and rescans(env) == [] and "replan_edits" not in decided(env)


# The tests below run the real tools on made-up files, see the layouts fixture.


def runs(monkeypatch):
    """The argv of each subprocess.run() call from now on."""
    seen, real = [], hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, *a, **k: seen.append(list(argv)) or real(argv, *a, **k))
    return seen


def writes(seen):
    """(mkvpropedit runs on the file, mkvmerge runs that write a file)."""
    return len([a for a in seen if a[0] == "mkvpropedit"]), len([a for a in seen if "mkvmerge" in a and "-o" in a])


def no_damage(monkeypatch):
    """The video check of a 30-second file runs no window, so it would block a repair. It finds no damage here."""
    monkeypatch.setattr(hook, "video_check", lambda path, again=False, j=None, hp=None: (
        None, [], {"header": dict(hp, repairable=True, blocked=[], windows_clean=True) if hp and hp["issue"] else hp}))


def made(layouts, tmp_path, name, tail=b""):
    """A copy of the layouts file name with tail after it, with its probe."""
    path = tmp_path / "Movie (2000)" / f"{name}.mkv"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(layouts[name].read_bytes() + tail)
    return str(path), hook.mkvmerge(str(path))


def no_room(src, dst):
    """src, ffmpeg's file, with the Void after its first SeekHead turned into an empty SeekHead that holds it. A grown
    SeekHead then has no room in front, so mkvpropedit moves Tracks past the Clusters and chains a second SeekHead."""
    b = bytearray(src.read_bytes())
    ds = D.segment_start(bytes(b[:64]))[0]
    sh = D.element(bytes(b), ds)
    v = D.element(bytes(b), sh[1] + sh[2])
    assert sh[0] == D.SEEKHEAD and v[0] == D.VOID, (sh, v)
    at, n = sh[1] + sh[2], v[1] + v[2] - sh[1] - sh[2]
    b[at:at + n] = D.SEEKHEAD.to_bytes(4, "big") + bytes([0x80 | n - 5, D.VOID, 0x80 | n - 7]) + bytes(n - 7)
    dst.write_bytes(b)
    return str(dst)


def edit_of(j, n=3, flag=1):
    """An edit of the forced flag of track n of j."""
    return [[f"track:={j['tracks'][n - 1]['properties']['uid']}", flag, 0, D.FORCED_FLAG]]


def test_a_flag_edit_of_a_file_with_room_runs_mkvpropedit_alone(layouts, tmp_path, settings, monkeypatch):
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    path, j = made(layouts, tmp_path, "src")
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edit_of(j), True)
    assert (rec["outcome"], writes(seen)) == ("edited", (1, 0)) and "header_repair" not in rec and "layout" not in rec, rec


def test_an_edit_that_breaks_the_layout_is_repaired_in_the_same_run(layouts, tmp_path, settings, monkeypatch):
    """mkvpropedit moves Tracks past the Clusters of a file with no room behind its front SeekHead. The header repair
    runs at once, after the signal block of mkvpropedit ended, and keeps the edit."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    (tmp_path / "Movie (2000)").mkdir()
    path = no_room(layouts["ffmpeg"], tmp_path / "Movie (2000)" / "chain.mkv")
    j = hook.mkvmerge(path)
    edits = edit_of(j, 5)   # the Spanish track, so the safety rule of the mute passes
    assert hook.layout_of(path) is None and hook.header_of(path, j)["issue"] == []
    no_damage(monkeypatch)
    real, blocked = hook.repack, []
    monkeypatch.setattr(hook, "repack", lambda *a, **k: blocked.append(signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, [])) or real(*a, **k))
    hook.mute_mark(path)   # a burn-in job turned the subtitles off, and the repair keeps its mark
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edits, True)
    assert (rec["outcome"], rec["header_repair"]["code"], writes(seen), blocked) == ("edited", "header_repaired", (1, 1), [False]), rec
    assert rec["header_repair"]["issue"] == [LAYOUT] and "layout" not in rec
    assert hook.layout_of(path) is None and D.unapplied(hook.mkvmerge(path), edits) == [] and hook.muted(path)
    assert [r.get("outcome", r["result"]) for r in log_lines(None)] == ["editing", "header_repaired"]


def test_a_layout_the_edit_broke_after_a_failed_repair_waits(layouts, tmp_path, settings, monkeypatch):
    """A header repair that failed earlier in the run blocks a second one. The next header check repairs the layout."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    (tmp_path / "Movie (2000)").mkdir()
    path = no_room(layouts["ffmpeg"], tmp_path / "Movie (2000)" / "chain.mkv")
    j = hook.mkvmerge(path)
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edit_of(j), True, hp={"issue": [], "tried": "header repair failed: x"})
    assert (rec["outcome"], writes(seen)) == ("edited", (1, 0)) and "header_repair" not in rec and hook.layout_of(path) == TRACKS_LATE, rec


def test_a_layout_repair_leaves_a_subtitle_past_the_end_to_the_header_check(env, monkeypatch):
    """The trim of a subtitle past the end needs the runtime of the item, which only the header check reads."""
    monkeypatch.setattr(hook, "layout_of", lambda path: "Tracks sits after the first Cluster")
    monkeypatch.setattr(hook, "header_of", lambda path, j=None: dict(HEADER_HP, issue=[LAYOUT], trim=[3], remove=[]))
    monkeypatch.setattr(hook, "repack", lambda *a, **k: pytest.fail("no repair"))
    j = env["probe"]
    rec = hook.edit({"path": env["path"]}, j, [[D.classify(j)[0]["sel"], 0, 1]], True, hp={"issue": []})
    assert rec["outcome"] == "edited" and rec["header_repair"]["code"] == "header_not_repaired", rec
    assert rec["header_repair"]["result"] == f"not repaired: a subtitle runs past the end, so the next header check repairs it. The issue: {LAYOUT}"


@pytest.mark.parametrize("name", ["ffmpeg", "src"])
def test_a_flag_edit_of_a_file_with_bytes_past_the_segment_end_is_one_remux(layouts, tmp_path, settings, monkeypatch, name):
    """mkvpropedit would put the 700 zero bytes inside the Segment of ffmpeg's file, which has no Void after Tracks.
    mkvmerge's file has one, and mkvpropedit can still grow Tracks into a Void elsewhere. Both take the edit in one
    header repair, which drops the bytes."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    path, j = made(layouts, tmp_path, name, bytes(700))
    no_damage(monkeypatch)
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edit_of(j), True)
    assert (rec["outcome"], rec["header_repair"]["code"], writes(seen)) == ("edited", "tail_removed", (0, 1)), rec
    hp = hook.header_of(path)
    assert (hp["issue"], hp["edit_tail"], hp["enclosed"], hook.segment_short(path)) == ([], 0, None, 0), hp
    assert D.unapplied(hook.mkvmerge(path), edit_of(j)) == [] and hook.layout_of(path) is None


def test_zeros_an_older_edit_enclosed_take_the_repair_with_the_edit(layouts, tmp_path, settings, monkeypatch):
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    path, j = made(layouts, tmp_path, "ffmpeg", bytes(700))
    subprocess.run(["mkvpropedit", "-q", path, *hook.propedit_args(edit_of(j, 4), 1)], check=True)
    j = hook.mkvmerge(path)
    assert hook.header_of(path, j)["enclosed"] == os.path.getsize(layouts["ffmpeg"])
    no_damage(monkeypatch)
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edit_of(j), True)
    assert (rec["outcome"], rec["header_repair"]["code"], writes(seen)) == ("edited", "header_repaired", (0, 1)), rec
    hp = hook.header_of(path)
    assert (hp["issue"], hp["enclosed"]) == ([], None) and D.unapplied(hook.mkvmerge(path), edit_of(j) + edit_of(j, 4)) == []


def test_enclosed_zeros_with_damage_keep_the_flags(layouts, tmp_path, settings, monkeypatch):
    """Enclosed zeros are a header issue, so the video check looks for real damage first. Damage blocks the remux,
    and mkvpropedit would enclose more, so the run writes nothing."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    path, j = made(layouts, tmp_path, "ffmpeg", bytes(700))
    subprocess.run(["mkvpropedit", "-q", path, *hook.propedit_args(edit_of(j, 4), 1)], check=True)
    j, before = hook.mkvmerge(path), open(path, "rb").read()
    monkeypatch.setattr(hook, "video_check", lambda path, again=False, j=None, hp=None: (
        None, ["a blank gap"], {"header": dict(hp, repairable=False, blocked=["a blank gap"])}))
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edit_of(j), True)
    assert (rec["outcome"], writes(seen), open(path, "rb").read() == before) == ("edit_failed", (0, 0), True), rec
    assert rec["result"] == "not edited, as a flag edit would put a run of zero bytes inside the Matroska Segment: a blank gap"


def test_another_file_past_the_segment_end_keeps_the_flags(layouts, tmp_path, settings, monkeypatch):
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    path, j = made(layouts, tmp_path, "ffmpeg", layouts["src"].read_bytes()[:700])
    before = open(path, "rb").read()
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edit_of(j), True)
    assert (rec["outcome"], writes(seen)) == ("edit_failed", (0, 0)) and open(path, "rb").read() == before, rec
    assert rec["result"].endswith(JOINED), rec["result"]


@pytest.mark.parametrize("stage", [False, True])
def test_header_repair_off_edits_as_before(layouts, tmp_path, settings, monkeypatch, stage):
    """HEADER_REPAIR=false turns both guards off, also for a header stage the write stage hands on."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path), header_repair=False)
    path, j = made(layouts, tmp_path, "ffmpeg", bytes(700))
    seen = runs(monkeypatch)
    rec = hook.edit({"path": path}, j, edit_of(j), True, hp=hook.header_of(path, j) if stage else None)
    assert (rec["outcome"], writes(seen)) == ("edited", (1, 0)) and "header_repair" not in rec, rec



# The run after a repair inside edit(), and the guards that keep the write stage as safe as 2.18.3.


@pytest.mark.parametrize("fix", ["tail", "layout"])
def test_the_content_checks_run_after_a_repair_inside_the_flag_edit(env, monkeypatch, fix):
    """Fix 2 takes the edits of a file with a tail in one remux, and fix 1 repairs the layout that mkvpropedit broke.
    Either replaces the file. The run then probes the new file, so the metadata checks read it, and the decision line
    holds its size."""
    if fix == "tail":
        calls = header_issue(env, monkeypatch, issue=[], edit_tail=700, edit_blocked=None, result=("tail_removed", "tail removed"))
    else:   # the header check finds no issue, and the edit breaks the layout
        calls, fake = header_issue(env, monkeypatch), hook.header_of
        monkeypatch.setattr(hook, "header_of", lambda path, j=None, edit=False: fake(path, j, edit) if env["mkvpropedit"] else dict(fake(path, j, edit), issue=[]))
        monkeypatch.setattr(hook, "layout_of", lambda path: None if calls["done"] else TRACKS_LATE)
    hook.mute_mark(env["path"])   # a burn-in job turned the subtitles off before, and the new file keeps its mark
    hook.main([])
    rec = decided(env)
    assert len(calls["repairs"]) == 1 and rec["outcome"] == "edited" and rec["header_repair"]["rescan"] == "sent", rec
    assert "evidence" in rec and rec["size"] == os.path.getsize(env["path"]) and hook.muted(env["path"]), sorted(rec)


def test_a_replan_that_takes_back_every_carried_edit_records_no_change(env, monkeypatch):
    """The Lavf file plans the first English track on. mkvmerge's tags show it forced and the second one full, which is
    already on, so mkvpropedit takes the carried edit back. The file holds the flags of the original, and the record
    says no change, as 2.18.3 did after its remux."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Portuguese"}
    env["probe"] = copy.deepcopy(LAVF)
    env["probe"]["tracks"][3]["properties"]["default_track"] = True
    first = D.decide(env["probe"], "Portuguese")["edits"]
    after = hook.planned_state(env["probe"], first)[0]
    after["container"]["properties"]["writing_application"] = "mkvmerge v102.0"
    for t, n in zip(after["tracks"][2:], ("50", "1500")):
        t["properties"]["tag_number_of_frames"] = n
    header_issue(env, monkeypatch, after=after)
    hook.main([])
    rec = decided(env)
    assert first == [["track:=3", 1, 0], ["track:=4", 0, 1]] and len(env["mkvpropedit"]) == 1, env["mkvpropedit"]
    assert (rec["outcome"], rec["result"], rec["replan_edits"], "edits" in rec) == ("no_change", "header repaired", 2, False), rec
    assert not hook.hook_edit_failed(env["path"])   # the take-back's mkvpropedit ended, so no editing mark stays


def test_flag_hearing_keeps_the_tracks_the_remux_takes_out_unmatched(monkeypatch):
    """flag_hearing() decides again as subtitle_checks() does: a garbled track the remux takes out gets no flag."""
    j = sub_probe(("eng", False, {}), ("pol", True, {}), audio=(("eng", True),))
    got = []
    real = D.decide
    monkeypatch.setattr(D, "decide", lambda *a, **k: got.append(set(a[7]) if len(a) > 7 else None) or real(*a, **k))
    ctx = types.SimpleNamespace(content_lang="eng", flag_hear={"a1"}, rec={}, d=real(j, "English"), sync={"s1": {"verdict": "match", "audio": 0}},
                                got={}, checked={}, j=j, shared=False, original="English", kids=False, release="", heard={}, spoken=None,
                                wrong={}, unmatched={"s1"}, strip={"s2": "pol"}, known=set(), said=set(), read={})
    hook.flag_hearing(ctx)
    assert ctx.rec["flag_matched"] == {"a1": "eng"} and got == [{"s1", "s2"}], (ctx.rec, got)


def test_unedited_takes_back_both_language_tags():
    """A carried language edit sets the legacy tag and the BCP 47 tag. The probe without it holds the original's of
    both, and no BCP 47 tag where the original had none."""
    old = sub_probe(("und", False, {}), audio=(("eng", True),))
    edits = [[D.classify(old)[1]["sel"], "fre", "und", D.LANG_EDIT]]
    new = hook.planned_state(old, edits)[0]
    new["tracks"][2]["properties"]["language"] = "fre"
    back = hook.unedited(new, old, edits)["tracks"][2]["properties"]
    assert (back.get("language"), "language_ietf" in back) == ("und", False), back


def test_a_failed_tail_remux_inside_the_flag_edit_keeps_the_flags(env, monkeypatch):
    calls = header_issue(env, monkeypatch, issue=[], edit_tail=700, edit_blocked=None, result=("header_repair_failed", "header repair failed: mkvmerge exited 2"))
    hook.main([])
    rec = decided(env)
    assert len(calls["repairs"]) == 1 and env["mkvpropedit"] == [] and rec["outcome"] == "edit_failed", rec
    assert rec["result"] == f"not edited, as {TAIL}: header repair failed: mkvmerge exited 2" and rec["after"] == rec["before"], rec["result"]


def test_the_mute_rule_holds_the_edits_a_remux_carries(env, monkeypatch):
    """A burn-in job turned the English subtitles off. A plan that turns them on again breaks the safety rule, also when
    a remux would carry the edit. muted_plan() normally leaves such an edit out, so the test takes it away."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Portuguese"}
    env["probe"] = sub_probe(("eng", False, {}), audio=(("por", True),))
    calls = header_issue(env, monkeypatch)
    monkeypatch.setattr(hook, "muted_plan", lambda plan, muted=True: plan)
    hook.mute_mark(env["path"])
    with pytest.raises(hook.arr_subsync.Broken, match="rule muted broken"):
        hook.main([])
    assert calls["repairs"] == [] and env["mkvpropedit"] == []


def test_a_header_repair_before_the_checks_hands_the_edit_a_fresh_stage(env, monkeypatch):
    """The repair before the checks dropped the tail too. The flag edit reads the new file, so mkvpropedit runs, and no
    second remux."""
    calls = header_issue(env, monkeypatch, issue=HEADER_HP["issue"], first=True, edit_tail=700)
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [(True, [])] and len(env["mkvpropedit"]) == 1 and rec["outcome"] == "edited", calls


def test_a_failed_header_repair_before_the_checks_tries_no_second_one(env, monkeypatch):
    calls = header_issue(env, monkeypatch, issue=HEADER_HP["issue"], first=True, result=("header_repair_failed", "header repair failed: x"))
    monkeypatch.setattr(hook, "layout_of", lambda path: TRACKS_LATE)
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [(True, [])] and len(env["mkvpropedit"]) == 1 and rec["header_repair"]["code"] == "header_repair_failed", calls


def test_a_deep_analysis_checks_edit_blocked_before_its_remux(env, monkeypatch, settings):
    """A deep analysis reads no header stage. The write stage then probes the file itself, and another Matroska file past
    the Segment end stops the subtitle remux, which would drop it."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    calls = header_issue(env, monkeypatch, issue=[], edit_tail=700, edit_blocked=None, on_edit=[JOINED])
    hook.deep_analysis(queue_analysis(env, env["path"]), [])
    rec = [r for r in log_lines(env) if r.get("source") == "deep_analysis" and "schema" in r][-1]
    assert got == [] and calls["edit"] == [False, True] and rec["subremux"]["codes"] == ["subtitle_remux_skipped"], (got, rec.get("subremux"))


def stop_in_layout_repair(layouts, tmp_path, settings, monkeypatch, sig, handler):
    """edit() on a file whose front SeekHead has no room, so mkvpropedit breaks the layout and fix 1 repairs it. sig
    arrives while the repair writes its temp file, with handler as the process's own handler of sig. Returns (the
    record, the path, the edits)."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    monkeypatch.setattr(hook, "STOP", {"held": False, "asked": False, "term": None})
    (tmp_path / "Movie (2000)").mkdir()
    path = no_room(layouts["ffmpeg"], tmp_path / "Movie (2000)" / "chain.mkv")
    j = hook.mkvmerge(path)
    no_damage(monkeypatch)
    real = hook.new_tmp
    monkeypatch.setattr(hook, "new_tmp", lambda tmp, st, excl=False: real(tmp, st, excl) or os.kill(os.getpid(), sig))
    old = signal.signal(sig, handler)
    try:
        return hook.edit({"path": path}, j, edit_of(j, 5), True), path, edit_of(j, 5)
    finally:
        signal.signal(sig, old)


def test_a_stop_during_the_layout_repair_lets_a_job_run_to_its_end(layouts, tmp_path, settings, monkeypatch):
    """A job process runs job_term(). SIGTERM during fix 1's remux removes its temp file and keeps the edit. The job
    then runs to its end and writes its decision line, as after any edit."""
    rec, path, edits = stop_in_layout_repair(layouts, tmp_path, settings, monkeypatch, signal.SIGTERM, hook.job_term)
    assert (rec["outcome"], rec["header_repair"]["code"], hook.STOP) == ("edited", "header_not_repaired", {"held": True, "asked": True, "term": None}), rec
    assert rec["header_repair"]["result"].startswith("not repaired: the run was stopped. ") and hook.layout_of(path) == TRACKS_LATE
    assert D.unapplied(hook.mkvmerge(path), edits) == [] and not os.path.exists(hook.repack_tmp(path))


def test_ctrl_c_during_the_layout_repair_of_a_lone_run_stops_its_next_write(layouts, tmp_path, settings, monkeypatch):
    """A run with Python's own Ctrl+C handler, as --check, keeps the stop. Its next write raises it, see raise_term()."""
    rec, path, edits = stop_in_layout_repair(layouts, tmp_path, settings, monkeypatch, signal.SIGINT, signal.default_int_handler)
    assert (rec["outcome"], rec["header_repair"]["code"], hook.STOP["term"]) == ("edited", "header_not_repaired", signal.SIGINT), rec
    with pytest.raises(KeyboardInterrupt):
        hook.raise_term()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_during_the_layout_repair_reaches_the_backfill(layouts, tmp_path, settings, monkeypatch, sig):
    """A backfill's own handler takes SIGTERM and Ctrl+C, and the backfill ends after the file. The repair passes the
    stop on to it, so the first stop is never lost, and the run writes the line of its edit."""
    got = []
    rec, path, edits = stop_in_layout_repair(layouts, tmp_path, settings, monkeypatch, sig, lambda s, _: got.append(s))
    assert (rec["outcome"], rec["header_repair"]["code"], got, hook.STOP["term"]) == ("edited", "header_not_repaired", [sig], None), rec


def test_a_repair_inside_a_burn_in_edit_keeps_the_queued_jobs_of_the_file(env, monkeypatch, settings):
    """The deep analysis and the whole-file check of the import hold the key of its file. A repair inside the burn-in
    job's edit replaces the file, so they take the key of the new file and do not drop."""
    settings(subtitles="deep")
    name = queue_analysis(env, env["path"])
    before = os.stat(env["path"])
    with open(env["path"] + ".new", "wb") as f:
        f.write(open(env["path"], "rb").read() + b"r")
    os.replace(env["path"] + ".new", env["path"])
    other = hook.whole_name(env["path"])   # queued by a newer import of the path, for another file
    hook.store.write("INSERT INTO jobs (name, at, job) VALUES (?, ?, ?)", other, 0.0, json.dumps({"path": env["path"], "key": [1, 2, 3]}))
    hook.rekey(env["path"], before)
    assert hook.job_of(name)["key"] == list(hook.file_key(os.stat(env["path"]))) and hook.deep_replaced(hook.job_of(name)) is None
    assert hook.job_of(other)["key"] == [1, 2, 3]


@pytest.mark.parametrize("edits", [False, True, "repair"])
def test_a_file_the_app_removes_after_the_write_keeps_its_line(env, monkeypatch, edits):
    """The app removes the file right after the write, as an upgrade of the item does, also after a repair inside the
    edit. The run ends with its decision line, no change or edited, and the checks after the write skip the file, as in
    2.18.3."""
    if not edits:
        monkeypatch.setattr(hook, "needed", lambda j, e: [])
    if edits == "repair":
        header_issue(env, monkeypatch, issue=[], edit_tail=700, edit_blocked=None, result=("tail_removed", "tail removed"))
    real = hook.edit
    monkeypatch.setattr(hook, "edit", lambda rec, *a, **k: [real(rec, *a, **k), os.remove(rec["path"])][0])
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == ("edited" if edits else "no_change") and "evidence" not in rec, rec["result"]


@pytest.mark.parametrize("first", [False, True])
def test_a_file_the_app_replaces_after_the_write_gets_no_content_checks(env, monkeypatch, first):
    """Another file at the path is no file this run decided, so the checks after the write skip it, as in 2.18.3. A
    header repair before the checks is no repair of the flag edit, so it changes nothing there."""
    monkeypatch.setattr(hook, "needed", lambda j, e: [])
    if first:
        calls = header_issue(env, monkeypatch, issue=HEADER_HP["issue"], first=True)
    real = hook.edit

    def edit(rec, *a, **k):
        out, p = real(rec, *a, **k), rec["path"]
        with open(p + ".app", "wb") as f:
            f.write(open(p, "rb").read() + b"app")
        os.replace(p + ".app", p)
        return out
    monkeypatch.setattr(hook, "edit", edit)
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "no_change" and "evidence" not in rec and rec["size"] == 1000 + first, rec
    assert not first or calls["repairs"] == [(True, [])]


def test_a_repair_inside_a_burn_in_edit_re_keys_the_jobs_and_an_app_change_does_not(env, burn, monkeypatch, settings):
    """The burn-in mute's edit replaces the file only through a repair of its own. Then the queued deep analysis takes
    the new key. A file the app replaced during the edit is another file, so the job keeps its key and drops."""
    settings(subtitles="deep")
    name = japanese_burn(env, ("eng", True, {}))
    deep = queue_analysis(env, env["path"])
    burn["full"] = [verdict("full", True, "eng")]
    real, repaired = hook.edit, {"code": "tail_removed"}

    def edit(rec, *a, **k):   # the file under a new inode, through a repair of the edit's own or through the app
        out, p = real(rec, *a, **k), rec["path"]
        with open(p + ".new", "wb") as f:
            f.write(open(p, "rb").read() + b"r")
        os.replace(p + ".new", p)
        return dict(out, header_repair=repaired) if repaired else out
    monkeypatch.setattr(hook, "edit", edit)
    hook.background(name, [])
    assert hook.job_of(deep)["key"] == list(hook.file_key(os.stat(env["path"])))
    key, repaired = hook.job_of(deep)["key"], None
    env["files"].pop(env["path"])   # the English subtitles are on again
    hook.background(japanese_burn(env, ("eng", True, {})), [])
    assert len(env["mkvpropedit"]) == 2
    assert hook.job_of(deep)["key"] == key != list(hook.file_key(os.stat(env["path"])))


def test_a_failed_take_back_stays_an_edit_failure(env, monkeypatch):
    """mkvpropedit fails to take back the carried edits, so the record keeps the failure, never a no change."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Portuguese"}
    env["probe"] = copy.deepcopy(LAVF)
    env["probe"]["tracks"][3]["properties"]["default_track"] = True
    first = D.decide(env["probe"], "Portuguese")["edits"]
    after = hook.planned_state(env["probe"], first)[0]
    after["container"]["properties"]["writing_application"] = "mkvmerge v102.0"
    for t, n in zip(after["tracks"][2:], ("50", "1500")):
        t["properties"]["tag_number_of_frames"] = n
    header_issue(env, monkeypatch, after=after)
    real = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: type("R", (), {"returncode": 2, "stdout": "", "stderr": "Error: no write"})()
                        if argv[0] == "mkvpropedit" else real(argv, **kw))
    hook.main([])
    assert decided(env)["outcome"] == "edit_failed"


def test_re_keying_a_file_the_app_removed_changes_nothing(env):
    before = os.stat(env["path"])
    os.remove(env["path"])
    hook.rekey(env["path"], before)
