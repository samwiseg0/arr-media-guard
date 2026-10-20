# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""A variable frame rate: the share of frame steps off the usual frame length, see decide.off_rate(), its alert at
import, and the header repair's proof of every frame of such a track, see remux.same_frames(). Every file is made up."""
import os
import shutil

import pytest

import amg
from test_arr_media_guard import (BAD_WINDOW, CLEAN_WINDOW, REAL_MKVMERGE, REAL_RUN, backfill_job, check_listed, checked, enqueue, env,  # noqa: F401
                                  hook, log_lines, plex_analyzed, posted, state_dir_in_tmp)
decide = hook.arr_decide

SECS = 70   # over 60 s, so the video check and the proof run their windows


@pytest.fixture(scope="module")
def vfr(tmp_path_factory):
    """A made-up 70 s H.264 and AAC file whose frames run at 24000/1001 and 30000/1001 fps in turn, 5 s each, with a
    DefaultDuration of 30000/1001 fps and no Cues: vfr.mkv. A HandBrake file of a telecined source looks so. cfr.mkv
    holds the same frames at a constant 30000/1001 fps. The end
    over the default duration wants 2098 frames, and the file holds 1888. lost.mkv is vfr.mkv with its cluster at 40 s
    made a Void element, so a read of it drops that cluster with no warning, as mkvmerge drops a damaged one."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d, t, stamps = tmp_path_factory.mktemp("vfr"), 0.0, ["# timestamp format v2"]
    while t < SECS * 1000:
        stamps.append(f"{t:.3f}")
        t += 1001 / (24 if int(t // 5000) % 2 == 0 else 30)
    (d / "ts.txt").write_text("\n".join(stamps) + "\n")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-frames:v", str(len(stamps) - 1), "-c:v", "libx264",
                "-preset", "ultrafast", "-g", "48", "-threads", "1", "-f", "h264", str(d / "v.h264")], check=True)
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={SECS}", "-c:a", "aac", str(d / "a.m4a")], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "vfr.mkv"), "--cues", "0:none", "--default-duration", "0:30000/1001p", "--timestamps", f"0:{d / 'ts.txt'}",
              str(d / "v.h264"), "--cues", "0:none", str(d / "a.m4a")], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "cfr.mkv"), "--default-duration", "0:30000/1001p", str(d / "v.h264"), str(d / "a.m4a")], check=True)
    b = bytearray((d / "vfr.mkv").read_bytes())
    p = decide.segment_start(b)[0]
    while True:   # the first cluster at 40 s or later
        i, at, n = decide.element(b, p)
        if i == decide.CLUSTER:
            _, ts, k = decide.element(b, at)   # its Timestamp, the first child
            if int.from_bytes(b[ts:ts + k], "big") >= 40000:
                break
        p = at + n
    b[p:p + 9] = bytes([decide.VOID]) + (1 << 56 | at + n - p - 9).to_bytes(8, "big")
    (d / "lost.mkv").write_bytes(b)
    return d


def test_the_frame_steps_tell_a_variable_rate_from_lost_frames():
    """A lost cluster makes one long step, and a frame the encoder left out one step of two frames. A constant rate
    that lost frames in a few places so stays under VFR_SHARE, and so does an encoder's jitter of 2 ms. Mixed rates
    put most steps off the usual frame length, and so do repeated frames an encoder dropped all through the track.
    The frames of a track with B-frames come in decode order, so the steps count in time order."""
    frame = 1001 / 24000
    whole = [round(k * frame, 3) for k in range(2400)]
    lost = whole[:1000] + whole[1100:]                     # a cluster of 100 frames
    holes = [t for k, t in enumerate(whole) if k % 300 != 7]   # 8 frames the encoder left out
    jitter = [t + (0.002 if k % 5 == 2 else 0) for k, t in enumerate(whole)]
    mixed = [round(k * 1001 / 30000, 3) for k in range(600)] + [round(600 * 1001 / 30000 + k * frame, 3) for k in range(1, 600)]
    dropped = [t for k, t in enumerate(whole) if k % 3]   # every third frame dropped
    decoded = whole[:1] + [whole[i] for g in range(1, 2398, 3) for i in (g + 2, g, g + 1)]   # B-frames: file order is decode order
    share = lambda times: decide.off_rate(decide.frame_steps(times))
    assert share(whole) == 0 and share(jitter) == 0 and share(decoded) == 0
    assert share(lost) < decide.VFR_SHARE and share(holes) < decide.VFR_SHARE
    assert share(mixed) > 0.4 and share(dropped) > 0.3
    assert share([1.0, float("nan"), None]) is None


def test_the_video_check_logs_a_variable_frame_rate(vfr, tmp_path, settings):
    """The steps between the frames of the three windows, against their median."""
    settings(log=str(tmp_path / "log.jsonl"))
    assert hook.video_check(str(vfr / "vfr.mkv"))[2]["frame_rate"]["variable"] is True
    assert hook.video_check(str(vfr / "cfr.mkv"))[2]["frame_rate"] == {"off": 0.0, "variable": False}


def repair(path):
    """The header repair of path as an import runs it: the video check, then repack() on its header stage."""
    j = REAL_MKVMERGE(str(path))
    return hook.repack(str(path), j, os.stat(path), True, hook.video_check(str(path), j=j)[2]["header"])


def test_a_variable_rate_remux_that_keeps_every_frame_is_written(vfr, tmp_path, settings):
    """The end over the default duration wants 2098 frames, and the file holds 1888. The frame steps show a variable
    rate, so the proof counts the original's frames instead. The new file holds each one at its time, and replaces
    the original."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "vfr.mkv"
    shutil.copy(vfr / "vfr.mkv", path)
    code, result, info = repair(path)
    assert result == "header repaired", (result, info)
    assert info["frames"] == 1888 and info["frame_rate"]["variable"] and info["frame_rate"]["original_frames"] == 1888, info
    assert hook.header_of(str(path))["issue"] == []


def test_a_variable_rate_remux_that_lost_a_cluster_is_refused(vfr, tmp_path, settings, monkeypatch):
    """mkvmerge drops a damaged cluster with no warning. Here the remux reads lost.mkv, whose cluster at 40 s is a
    Void, in place of the original. The new file then holds fewer frames than the original, so the proof refuses,
    and the original stays."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "vfr.mkv"
    shutil.copy(vfr / "vfr.mkv", path)
    before, real = path.read_bytes(), hook.merge_argv
    monkeypatch.setattr(hook, "merge_argv", lambda src, *a, **k: [str(vfr / "lost.mkv") if x == src else x for x in real(src, *a, **k)])
    code, result, info = repair(path)
    assert result.startswith("header repair failed: the video has a variable frame rate, and stream video 0 (h264) holds ") and \
        result.endswith(" timed packets in the new file, 1888 in the original"), (result, info)
    assert path.read_bytes() == before


def test_a_variable_rate_remux_of_a_damaged_original_is_refused(vfr, tmp_path, settings):
    """A zero run of 64 KiB inside a cluster of the original. mkvmerge drops the frames there with no warning and exit
    0, and ffprobe names the run. The proof reads the original clean, so it refuses on that line."""
    settings(log=str(tmp_path / "log.jsonl"))
    b = bytearray((vfr / "vfr.mkv").read_bytes())
    b[len(b) // 2:len(b) // 2 + (64 << 10)] = bytes(64 << 10)
    path = tmp_path / "vfr.mkv"
    path.write_bytes(b)
    code, result, info = repair(path)
    assert result.startswith("header repair failed: ffprobe did not read the packets of vfr.mkv: [matroska"), (result, info)
    assert info["warnings"] is None and info["frames"] == 1867 and path.read_bytes() == b, info   # 21 frames lost


def test_a_clean_read_refuses_damage_that_ffprobe_names(vfr, tmp_path):
    """ffprobe reads on past a zero run inside a cluster, and names it. Only the clean read refuses."""
    b = bytearray((vfr / "vfr.mkv").read_bytes())
    b[len(b) // 2:len(b) // 2 + (64 << 10)] = bytes(64 << 10)
    (tmp_path / "hole.mkv").write_bytes(b)
    assert hook.packet_places(str(tmp_path / "hole.mkv"), str(tmp_path), 600)
    with pytest.raises(RuntimeError, match=r"^ffprobe did not read the packets of hole.mkv: \[matroska"):
        hook.packet_places(str(tmp_path / "hole.mkv"), str(tmp_path), 600, clean=True)


MIXED = dict(CLEAN_WINDOW, steps={41.7: 70, 33.4: 50})   # a window of two frame lengths
ALERT = ("Variable frame rate", "The video has a **variable frame rate**. 42% of the frames checked stay on the screen longer or shorter than "
         "most frames. A player that follows the frame times plays it right. Nothing was changed.")


@pytest.mark.parametrize("windows, titles", [([MIXED], [ALERT[0]]), ([dict(CLEAN_WINDOW, steps={41.7: 120})], []),
                                             ([dict(MIXED, errors=4), MIXED, MIXED], ["Video may be broken"])], ids=["mixed", "constant", "doubt"])
def test_an_import_alerts_on_a_variable_frame_rate_alone(env, windows, titles):
    """The windows show two frame lengths, 41.7 and 33.4 ms, and the import posts one amber alert. It changes nothing for
    it. A constant rate posts nothing. A bad window is a doubt, and its alert posts in place of the rate."""
    env["window_out"] = windows
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    rec = [r for r in log_lines(env) if "schema" in r][-1]
    assert rec["source"] == "hook" and rec["video"]["frame_rate"]["variable"] == (windows[-1] is MIXED), rec["video"]
    got = [(t, text.replace("\n", " ")) for t, text in posted(env)]   # the embed breaks lines between sentences
    assert [t for t, _ in got] == titles and ("frame_rate" in rec["alert_kinds"]) == (titles == [ALERT[0]]), (got, rec["alert_kinds"])
    assert got == [ALERT] or titles != [ALERT[0]]


def test_a_backfill_job_and_check_log_a_variable_frame_rate_and_post_nothing(env, monkeypatch):
    """A bare backfill --apply queues each file as a --check --apply job, so a library pass would post one alert per
    file. The job, --check and --check --apply keep the rate in the decision line, and post nothing for it."""
    check_listed(env, monkeypatch)
    env["window_out"] = [MIXED]
    hook.background(backfill_job(env, monkeypatch), [])
    for apply in ([], ["--apply"]):
        hook.main(["--check", env["path"], *apply])
    recs = checked(env, "backfill_job") + checked(env)
    assert [r["video"]["frame_rate"] for r in recs] == [{"off": 0.4167, "variable": True}] * 3, recs
    assert not any("frame_rate" in (r.get("alert_kinds") or []) for r in recs) and not any(t == ALERT[0] for t, _ in posted(env))
