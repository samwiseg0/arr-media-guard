# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""A variable frame rate: the share of frame steps off the usual frame length, see decide.off_rate(), its alert at
import, and the header repair's proof of every frame the original stores, see remux.frames_fault(). Every file is made
up."""
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
    made a Void element, so a read of it drops that cluster with no warning, as mkvmerge drops a damaged one.
    drop.mkv holds frames at 24000/1001 fps with every third one left out, as an encoder that drops repeated frames
    writes them. Its DefaultDuration is 24000/1001 fps and it has no Cues."""
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
    kept = [f"{k * 1001 / 24:.3f}" for k in range(round(SECS * 24000 / 1001)) if k % 3 != 2]
    (d / "drop.txt").write_text("# timestamp format v2\n" + "\n".join(kept) + "\n")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24", "-frames:v", str(len(kept)), "-c:v", "libx264",
                "-preset", "ultrafast", "-g", "48", "-threads", "1", "-f", "h264", str(d / "d.h264")], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "drop.mkv"), "--cues", "0:none", "--default-duration", "0:24000/1001p", "--timestamps", f"0:{d / 'drop.txt'}",
              str(d / "d.h264"), "--cues", "0:none", str(d / "a.m4a")], check=True)
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
    skip = lambda times: decide.off_rate(decide.frame_steps(times), skips=True)   # the import's verdict
    assert skip(mixed) > 0.4 and skip(dropped) == 0 and skip(holes) == 0 and skip(jitter) == 0 and skip(lost) < decide.VFR_SHARE


def test_frames_left_out_at_the_joins_of_a_web_stream_keep_the_rate():
    """A 60 fps web stream stored to the ms steps 16 and 17 ms. At its joins it left a frame out, a step of 33 or 34
    ms, and in a few places a 5 ms gap, a step of 22 ms. Three windows of 297 steps hold 6 steps of two frames and 3
    of 22 ms, 1.01 percent off. With skips a step of two frames is on the rate, so 0.34 percent stays off. A step of
    1.5 frames from a DVD's repeated fields, of 2.5 frames, and a step under half a frame stay off. The bound stays
    VFR_STEP of one frame, so a step of 76 ms, 7.4 ms off two frames of 41.7 ms, stays off."""
    web = {17.0: 589, 16.0: 293, 33.0: 4, 34.0: 2, 22.0: 3}
    assert decide.off_rate(web) >= decide.VFR_SHARE and decide.off_rate(web, skips=True) == 3 / 891
    assert decide.off_rate({41.7: 600, 62.6: 200}, skips=True) == 0.25
    assert decide.off_rate({16.7: 600, 41.7: 20, 1.0: 20}, skips=True) == 40 / 640
    assert decide.off_rate({41.7: 600, 76.0: 100}, skips=True) == 100 / 700


def test_a_median_step_of_0_ms_counts_each_step_as_one_frame():
    """frame_steps() rounds a step to 0.1 ms, so steps of 0.04 ms give a median of 0.0 ms. With skips, each step then
    counts as one frame, as without skips, and the video check gets a share."""
    steps = decide.frame_steps([0.0, 0.00004, 0.00008, 0.00012, 0.0418])
    assert steps == {0.0: 3, 41.7: 1} and decide.off_rate(steps, skips=True) == decide.off_rate(steps) == 0.25


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
    """The end over the default duration wants 2098 frames, and the file holds 1888. The walk of the original's blocks
    counts 1888, and the new file holds each one at its time, so it replaces the original."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "vfr.mkv"
    shutil.copy(vfr / "vfr.mkv", path)
    code, result, info = repair(path)
    assert result == "header repaired", (result, info)
    assert info["frames"] == 1888 and info["frame_rate"]["variable"] and info["original_frames"] == 1888, info
    assert hook.header_of(str(path))["issue"] == []


def test_a_repair_of_a_track_that_dropped_repeated_frames_compares_every_frame(vfr, tmp_path, settings):
    """drop.mkv holds two thirds of the frames its DefaultDuration gives. The walk of the original counts the frames it
    stores, and the new file holds each one at its time, so the repair passes. Half its steps are two frames, so the
    repair counts its rate as variable."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "drop.mkv"
    shutil.copy(vfr / "drop.mkv", path)
    code, result, info = repair(path)
    assert result == "header repaired", (result, info)
    assert info["frame_rate"]["variable"] and info["original_frames"] == info["frames"] < SECS * 24000 / 1001 * 0.7, info


def test_a_variable_rate_remux_that_lost_a_cluster_is_refused(vfr, tmp_path, settings, monkeypatch):
    """mkvmerge drops a damaged cluster with no warning. Here the remux reads lost.mkv, whose cluster at 40 s is a
    Void, in place of the original. The new file then holds fewer frames than the original stores, so the repair
    refuses, and the original stays."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "vfr.mkv"
    shutil.copy(vfr / "vfr.mkv", path)
    before, real = path.read_bytes(), hook.merge_argv
    monkeypatch.setattr(hook, "merge_argv", lambda src, *a, **k: [str(vfr / "lost.mkv") if x == src else x for x in real(src, *a, **k)])
    code, result, info = repair(path)
    assert result.startswith("header repair failed: the new file holds ") and result.endswith(" video frames, and the original stores 1888"), (result, info)
    assert path.read_bytes() == before


def test_a_variable_rate_remux_of_a_damaged_original_is_refused(vfr, tmp_path, settings):
    """A zero run of 64 KiB inside a cluster of the original. mkvmerge drops the frames there with no warning and exit
    0. The walk of the original's blocks stops at the run, so the frames of the original are not all known, and the
    repair refuses with the walk's reason."""
    settings(log=str(tmp_path / "log.jsonl"))
    b = bytearray((vfr / "vfr.mkv").read_bytes())
    b[len(b) // 2:len(b) // 2 + (64 << 10)] = bytes(64 << 10)
    path = tmp_path / "vfr.mkv"
    path.write_bytes(b)
    code, result, info = repair(path)
    assert result.startswith("header repair failed: the video frames of the original are not all known, so lost frames cannot be counted: "
                             "the block walk of vfr.mkv failed: the Cluster at byte "), (result, info)
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
JOINS = [dict(CLEAN_WINDOW, steps=s) for s in ({17.0: 196, 16.0: 98, 22.0: 1, 33.0: 1, 34.0: 1}, {17.0: 196, 16.0: 98, 33.0: 1, 22.0: 1, 34.0: 1},
                                              {17.0: 197, 16.0: 97, 33.0: 2, 22.0: 1})]   # 60 fps that left a frame out at some joins
ALERT = ("Variable frame rate", "The video has a **variable frame rate**. 42% of the frames checked stay on the screen longer or shorter than "
         "most frames. A player that follows the frame times plays it right. Nothing was changed.")


@pytest.mark.parametrize("windows, titles", [([MIXED], [ALERT[0]]), ([dict(CLEAN_WINDOW, steps={41.7: 120})], []),
                                             ([dict(MIXED, errors=4), MIXED, MIXED], ["Video may be broken"]), (JOINS, [])],
                         ids=["mixed", "constant", "doubt", "joins"])
def test_an_import_alerts_on_a_variable_frame_rate_alone(env, windows, titles):
    """The windows show two frame lengths, 41.7 and 33.4 ms, and the import posts one amber alert. It changes nothing for
    it. A constant rate posts nothing, and so does a constant rate that left a frame out at a few joins. A bad window
    is a doubt, and its alert posts in place of the rate."""
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


@pytest.mark.parametrize("new, fault", [
    ([0.0, 0.04, 0.12, 0.16, 0.2], "the new file lacks 1 video frame the original stores, the first at 0.080 s"),
    ([0.0, 0.04, 0.08, 0.12], None),   # the last 2 frames, within FRAME_SLACK
    ([0.0, 0.04, 0.08, 0.13, 0.16, 0.2], "the MKV would show the video 10 ms out of sync with the audio, at the start"),
    ([0.0, 0.04], "the new file holds 2 video frames, and the original stores 6"),
    ([0.04, 0.08, 0.12, 0.16, 0.2], "the new file lacks 1 video frame the original stores, the first at 0.000 s"),   # even, no B-frames
    ([0.0, 0.04, 0.09, 0.12], "the MKV would show the video 10 ms out of sync with the audio, at the start"),   # 2 lost at the end, 1 moved
    ([0.0, 0.04, 0.08, 0.12, 0.16, 0.21], "the MKV would show the video 10 ms out of sync with the audio"),
])
def test_a_repair_names_the_first_frame_the_new_file_lacks(new, fault):
    """A new file that lost a frame in the middle pairs each later frame with the one before it. The reason then names
    the frame it lacks, never a move. Frames lost at the end pass up to FRAME_SLACK. A frame lost at the start
    refuses, as each frame keeps its own time. With every frame there, a moved frame refuses as moved. Two frames lost
    at the end and one moved name the move."""
    old = ([0.0, 0.04, 0.08, 0.12, 0.16, 0.2], 1000000)
    got = hook.frames_fault(old, new, 1000000, "h264")
    assert (got or "").startswith(fault or "") and bool(got) == bool(fault), got


@pytest.fixture(scope="module")
def holes(tmp_path_factory):
    """Made-up 70 s files of H.264 at 24000/1001 fps and AAC with no Cues, so a header repair runs. gaps.mkv: the encoder
    left out 10 frames at 5 places, and the video starts one frame late. The end over the default duration then wants
    11 frames more than the file holds. late.mkv: every stream starts 2.5 s late, and mkvmerge writes a Segment
    duration 2.5 s shorter than the stream end."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d = tmp_path_factory.mktemp("holes")
    gone = "+".join(f"between(n\\,{k}\\,{k + 1})" for k in (200, 500, 900, 1200, 1500))
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=24000/1001:duration={SECS}", "-vf", f"select='not({gone})'",
                "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-threads", "1", str(d / "v.mkv")], check=True)
    amg.ffmpeg(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={SECS}", "-c:a", "aac", str(d / "a.m4a")], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "gaps.mkv"), "--cues", "0:none", "--default-duration", "0:24000/1001p", "--sync", "0:42", str(d / "v.mkv"),
              "--cues", "0:none", str(d / "a.m4a")], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "late.mkv"), "--cues", "0:none", "--sync", "0:2500", str(d / "v.mkv"), "--cues", "0:none", "--sync", "0:2500",
              str(d / "a.m4a")], check=True)
    return d


@pytest.mark.parametrize("name", ["gaps.mkv", "late.mkv"])
def test_a_repair_keeps_frames_an_encoder_left_out_and_a_late_start(holes, tmp_path, settings, name):
    """The original stores the frames the new file must hold, so frames an encoder left out are no loss, and a video
    that starts one frame late needs no frame more. The streams of the new file end where the original's end, whatever
    the Segment duration says."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / name
    shutil.copy(holes / name, path)
    code, result, info = repair(path)
    assert result == "header repaired", (result, info)
    if name == "gaps.mkv":
        assert info["frames"] == info["original_frames"] < SECS * 24000 / 1001 - 9, info
    else:
        assert abs(info["new_end"] - SECS - 2.5) < 0.1 and abs(info["new_duration"] - SECS) < 0.1, info


def test_a_repair_names_the_first_lost_frame_in_file_order():
    """With B-frames the first frame in file order holds the smallest time. A new file that lost frames 0 to 2 names
    frame 0, at its own time. 14abe61 measured each list from its own first time and named 0.160 s."""
    order = [0, 3, 1, 2]   # an I or P frame, then the two B-frames it displays after, in file order
    old = ([(4 * (i // 4) + order[i % 4]) * 0.04 for i in range(40)], 1000000)
    assert hook.frames_fault(old, old[0][3:], 1000000, "h264") == "the new file lacks 3 video frames the original stores, the first at 0.000 s"
    assert hook.frames_fault(old, old[0][:20] + old[0][21:], 1000000, "h264") == "the new file lacks 1 video frame the original stores, the first at 0.800 s"
