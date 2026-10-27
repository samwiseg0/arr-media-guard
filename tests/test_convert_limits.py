# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Limits of the conversion to MKV, see convert.convert() and docs/design.md, "Conversion". An MP4 VobSub track with
no picture size refuses before the remux. A VobSub track with a size goes to the remux, and the proof decides. The
files are made up."""
import fcntl
import inspect
import os
import shutil
import struct
import subprocess
import sys

import pytest

import amg
from test_arr_media_guard import (REAL_MKVMERGE, REAL_RUN, check_listed, decided, env, hook, log_lines, mp4_import, pgs_sup,  # noqa: F401
                                  state_dir_in_tmp)

MP4 = {"container": {"type": "MP4/QuickTime"}}
VOB = lambda i, **size: {"index": i, "codec_type": "subtitle", "codec_name": "dvd_subtitle", **size}
WHY = "subtitle track 1 is VobSub with no picture size, and ffmpeg can't read such a track in an MKV"


def no_size(src, dst, sides=8):
    """A copy of the MP4 src whose subtitle track header holds the picture size 0x0, or with sides 4 a width of 0."""
    b = bytearray(src.read_bytes())

    def boxes(lo, hi):
        while lo + 8 <= hi:
            size, kind = struct.unpack(">I4s", b[lo:lo + 8])
            yield lo, size, kind
            lo += size
    moov = next((o, s) for o, s, k in boxes(0, len(b)) if k == b"moov")
    for o, s, k in boxes(moov[0] + 8, sum(moov)):
        kids = {kk: (oo, ss) for oo, ss, kk in boxes(o + 8, o + s)} if k == b"trak" else {}
        mdia = kids.get(b"mdia")
        if mdia and any(kk == b"hdlr" and b[oo + 16:oo + 20] == b"subp" for oo, ss, kk in boxes(mdia[0] + 8, sum(mdia))):
            t = kids[b"tkhd"][0]
            end = t + 8 + (88 if b[t + 8] == 1 else 76)   # the width and the height are the last 8 bytes of tkhd
            b[end:end + sides] = bytes(sides)
    dst.write_bytes(b)


@pytest.fixture(scope="module")
def vob_mp4(tmp_path_factory):
    """MP4 files of 10 seconds with H.264, AAC and a VobSub track of 160x90 that ffmpeg makes from PGS, one cue each
    2 s. gaps.mp4 shows each cue 1.5 s, and the MP4 sample lasts until the next cue. flush.mp4 shows each cue until the
    next one. zero.mp4 is gaps.mp4 with the picture size 0x0 in the subtitle track header, and wzero.mp4 with the
    width 0 alone."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvtoolnix")
    d = tmp_path_factory.mktemp("vob")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=10:size=160x90:rate=25", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=10", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac", str(d / "av.mp4")], check=True)
    for name, show in (("gaps", 1.5), ("flush", 2.0)):
        (d / f"{name}.sup").write_bytes(pgs_sup([(2.0 * i, 2.0 * i + show) for i in range(1, 5)]))
        REAL_RUN(["mkvmerge", "-q", "-o", str(d / f"{name}-pgs.mkv"), str(d / "av.mp4"), str(d / f"{name}.sup")], check=True)
        amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-fix_sub_duration", "-i", str(d / f"{name}-pgs.mkv"), "-map", "0", "-c:v", "copy", "-c:a", "copy",
                    "-c:s", "dvdsub", str(d / f"{name}-vob.mkv")], check=True)
        amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(d / f"{name}-vob.mkv"), "-map", "0", "-c", "copy", str(d / f"{name}.mp4")], check=True)
    no_size(d / "gaps.mp4", d / "zero.mp4")
    no_size(d / "gaps.mp4", d / "wzero.mp4", 4)
    return d


def convert_copy(vob_mp4, tmp_path, name, apply=True):
    """convert() of a copy of vob_mp4/<name>.mp4 as a file of --check that no app lists. Returns (code, result, the
    folder's names)."""
    path = tmp_path / "Film V (2001).mp4"
    shutil.copy(vob_mp4 / f"{name}.mp4", path)
    code, result, _, _ = hook.convert("radarr", str(path), REAL_MKVMERGE(str(path)), os.stat(path), apply, {}, outside=True, source="check")
    return code, result, sorted(os.listdir(tmp_path))


def test_an_mp4_vobsub_track_with_no_picture_size_refuses_before_the_remux(vob_mp4, tmp_path, settings):
    """mkvmerge writes "size: 0x0" into the MKV of such a track, or "size: 0x90" for a width of 0 alone, and ffprobe
    reads no stream of that MKV. The conversion refuses before the remux, so no temp file and no MKV appear, and the
    original stays. A dry run gives the same refusal."""
    settings(log=str(tmp_path / "log.jsonl"))
    for name in ("zero", "wzero"):
        for apply in (True, False):
            (tmp_path / f"{name}-{apply}").mkdir()
            assert convert_copy(vob_mp4, tmp_path / f"{name}-{apply}", name, apply) == ("repack_failed", f"repack failed: {WHY}", ["Film V (2001).mp4"])


def test_an_mp4_vobsub_track_with_a_size_goes_to_the_remux(vob_mp4, tmp_path, settings):
    """A VobSub track with a size converts when each MP4 sample lasts as long as its cue shows. mkvmerge 102 sets the
    stop time of each cue from the sample duration. With a gap after each cue, the MKV shows each cue until the next one,
    and the proof refuses after the remux."""
    settings(log=str(tmp_path / "log.jsonl"))
    for name, want in (("flush", "repacked"), ("gaps", "repack failed: the packet data of stream subtitle 2 (dvd_subtitle) differ")):
        (tmp_path / name).mkdir()
        code, result, names = convert_copy(vob_mp4, tmp_path / name, name)
        assert result == want, result
        assert names == ["Film V (2001).mkv", "Film V (2001).mp4"] if code == "repacked" else ["Film V (2001).mp4"], names


# B: take the gate, as runner.gated() does, then wait up to 6 s for the file lock, shared.
GATE_WAITER = """import fcntl, os, sys, time
gate = open(os.path.join(sys.argv[1], "lock.gate"), "w")
fcntl.flock(gate, fcntl.LOCK_EX)
print("gate", flush=True)
lock, end = open(os.path.join(sys.argv[1], "lock"), "w"), time.monotonic() + 6
while True:
    try:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        print("B got the lock", flush=True)
        break
    except BlockingIOError:
        if time.monotonic() > end:
            print("B gave up", flush=True)
            break
        time.sleep(0.05)
"""


@pytest.mark.parametrize("case", ["vobsub", "proof"])
def test_a_refused_import_never_waits_at_the_gate_with_the_lock(env, monkeypatch, case):
    """A hook import holds the file lock exclusive. After a conversion that failed, process.conversion() takes it again
    through the gate. B holds the gate there and waits for the lock. The refusal before the remux has let the lock
    go, as a proof refusal has, so B gets the lock and A then gets the gate. A run that held the lock would wait at the
    gate, and B would wait for the lock."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    if case == "vobsub":
        env["extra_streams"] = [VOB(3)]
    else:
        env["proof"] = ((0, "the packet data of stream video 0 (h264) differ"), [])
    seen, real = [], hook.gated

    def gated(f, op, *a, **k):
        if op == fcntl.LOCK_EX and inspect.stack()[1].function == "conversion":
            b = subprocess.Popen([sys.executable, "-I", "-c", GATE_WAITER, hook.CFG.state_dir], stdout=subprocess.PIPE, text=True)
            assert b.stdout.readline().strip() == "gate"
            try:
                real(f, op, type(hook.DEADLINE)(3, "A gave up at the gate"))
                seen.append("A got the gate")
            except Exception as ex:
                seen.append(f"A: {ex}")
            seen.append(b.stdout.read().strip())
            b.wait()
            return f
        return real(f, op, *a, **k)
    monkeypatch.setattr(hook, "gated", gated)
    hook.main([])
    assert decided(env)["outcome"] == "repack_failed" and seen == ["A got the gate", "B got the lock"], seen


@pytest.mark.parametrize("argv", [["--check", None], ["--backfill", "radarr", "--convert"]], ids=["check", "backfill"])
def test_a_dry_run_of_an_unsized_vobsub_file_keeps_its_shared_lock(env, monkeypatch, argv):
    """A dry run takes the file lock shared once and keeps it, for a file the conversion refuses before the remux
    as for any other file."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    check_listed(env, monkeypatch)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    env["extra_streams"] = [VOB(3)]
    ops, real = [], hook.gated
    monkeypatch.setattr(hook, "gated", lambda f, op, *a, **k: ops.append(op) or real(f, op, *a, **k))
    hook.main([a or mp4 for a in argv])
    outcomes = [r["outcome"] for r in log_lines(env) if "schema" in r and r.get("path") == mp4]
    assert "repack_failed" in outcomes and ops == [fcntl.LOCK_SH], (ops, outcomes)


def test_the_vobsub_refusal_names_each_track_without_a_size():
    """Only an MP4 refuses. A track with a width and no height has no size too. The number counts the subtitle tracks
    but the caption tracks. When ffprobe's count of them differs from mkvmerge's, the reason names no number."""
    mp4 = lambda n: dict(MP4, tracks=[{"type": "video"}] + [{"type": "subtitles"}] * n)
    cc = {"index": 2, "codec_type": "subtitle", "codec_name": "eia_608"}
    assert hook.vobsub_unsized(mp4(1), [cc, VOB(3, width=720, height=480)]) is None
    assert hook.vobsub_unsized({"container": {"type": "Matroska"}, "tracks": [{"type": "subtitles"}]}, [VOB(3)]) is None
    assert hook.vobsub_unsized(mp4(1), [cc, VOB(3, width=720)]) == WHY
    assert hook.vobsub_unsized(mp4(4), [VOB(2, height=480), VOB(3, width=720, height=480), VOB(4), VOB(5)]) == \
        "subtitle tracks 1, 3 and 4 are VobSub with no picture size, and ffmpeg can't read such a track in an MKV"
    assert hook.vobsub_unsized(mp4(0), [VOB(3)]) == "a VobSub track has no picture size, and ffmpeg can't read such a track in an MKV"
    assert hook.vobsub_unsized(mp4(3), [VOB(3), VOB(4)]) == "2 VobSub tracks have no picture size, and ffmpeg can't read such a track in an MKV"


def test_a_broken_track_pairs_as_the_vobsub_refusal_does(monkeypatch):
    """broken_tracks() pairs the subtitle streams of ffprobe with mkvmerge's tracks as vobsub_unsized() does, see
    convert.sub_streams(). A caption stream counts in neither. When the counts differ, no track is read."""
    monkeypatch.setattr(hook, "timed_packets", lambda path, indexes, streams: {i: [(1.0, 2.0, b"\xff\xfe")] for i in indexes})
    j = dict(MP4, tracks=[{"type": "subtitles", "id": 2, "properties": {"language": "fre"}}])
    tt, cc = {"index": 5, "codec_type": "subtitle", "codec_name": "mov_text"}, {"index": 6, "codec_type": "subtitle", "codec_name": "eia_608"}
    assert {p: (e["stream"], e["id"], e["lang"]) for p, e in hook.broken_tracks("/x/Film.mp4", j, [tt, cc]).items()} == {"s1": (5, 2, "fre")}
    assert hook.broken_tracks("/x/Film.mp4", j, [tt, dict(tt, index=7)]) == {}


def test_an_import_with_an_unsized_vobsub_track_alerts_the_refusal(env, monkeypatch):
    """The refusal of an import reaches the decision line and the alert, and no remux runs. ffprobe lists the VobSub
    track after the timed-text track, and mkvmerge lists the timed-text track alone, so the reason names no number."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    env["extra_streams"] = [VOB(3)]
    hook.main([])
    rec, why = decided(env), "a VobSub track has no picture size, and ffmpeg can't read such a track in an MKV"
    assert rec["outcome"] == "repack_failed" and rec["result"] == f"repack failed: {why}" and env["repacks"] == [] and os.path.exists(mp4), rec
    (text,) = [e["description"] for m, u, b in env["http"] if m == "POST" and "discord" in u for e in b["embeds"] if e["title"] == "Conversion to MKV failed"]
    assert f"Couldn't convert the MP4/QuickTime file to MKV, so the original was kept. A{why[1:]}." in text, text


ASS_BODY = ("ScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Default,Arial,24\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            + "".join(f"Dialogue: 0,0:00:0{i}.00,0:00:0{i}.50,Default,,0,0,0,,Line {i}\n" for i in range(1, 4)))
NO_SUBRIP = "mkvmerge cannot identify it as SubRip"
ASS = hook.arr_decide.ASS_TEXT


@pytest.mark.parametrize("head, why", [
    ("[Script Info]\n", ASS), ("﻿[Script Info]\n", ASS), ("\n \t\n[Script Info]\n", ASS),
    ("; a comment\n[Script Info]\n", ASS), ("  !: a comment\n[Script Info]\n", ASS), ("[script info]\n", ASS),
    ("  [Script   Info] more\n", ASS), ("[V4 Styles]\n", ASS), ("\n" * 98 + "[Script Info]\n", ASS),
    ("[ScriptInfo]\n", NO_SUBRIP), ("[ Script Info ]\n", NO_SUBRIP), ("[V4++ Styles]\n", NO_SUBRIP), ("# a note\n[Script Info]\n", NO_SUBRIP),
    ("Title: x\n[Script Info]\n", NO_SUBRIP), ("\n" * 100 + "[Script Info]\n", NO_SUBRIP), ("", NO_SUBRIP),
    ("\0abc\n[Script Info]\n", ASS), ("!x comment\n[Script Info]\n", ASS), ("\x0c\n[Script Info]\n", ASS), ("\x0b\n[Script Info]\n", ASS),
    ("\x0b[Script Info]\n", ASS), ("[Script Info]\0\n", ASS), ("\0[Script Info]\n", NO_SUBRIP), ("\xa0[Script Info]\n", NO_SUBRIP),
    ("\x1c[Script Info]\n", NO_SUBRIP), ("\u3000[Script Info]\n", NO_SUBRIP), ("[Script\xa0Info]\n", NO_SUBRIP), ("\xa0\n[Script Info]\n", NO_SUBRIP),
], ids=["plain", "bom", "blank lines", "comment", "bang comment", "lower case", "spaces", "v4 styles", "line 99", "no space", "inner spaces",
        "v4++", "hash", "key first", "line 101", "events first", "nul then text", "bang only", "form feed line", "vertical tab line",
        "vertical tab first", "nul after head", "nul first", "nbsp first", "x1c first", "ideographic space", "nbsp inside", "nbsp line"])
def test_misread_names_ass_text_as_mkvmerge_identifies_it(head, why):
    """A text that mkvmerge 102 identifies as SSA or ASS gives its own reason. Each shape gave SubStationAlpha in
    mkvmerge -J, or no recognized container (measured 2026-10-25, the review's shapes too). The [V4+ Styles] line of
    the body counts only when no other line comes first."""
    text = head + ASS_BODY if head or why is not NO_SUBRIP else ASS_BODY[ASS_BODY.index("[Events]"):]
    assert hook.misread(*hook.arr_decide.srt_read(text)) == why


def test_an_ass_text_sidecar_stays_beside_the_file_with_its_own_reason(env, monkeypatch):
    """A sidecar named .srt that holds an ASS script stays beside the new file byte for byte. The decision line and the
    notice say it holds ASS or SSA text under a .srt name."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=(".en.srt",))
    raw = ("[Script Info]\r\n" + ASS_BODY.replace("\n", "\r\n")).encode()
    side = mkv[:-4] + ".srt"
    with open(side, "wb") as f:
        f.write(raw)
    hook.main([])
    rec, name = decided(env), os.path.basename(side)
    assert rec["outcome"] == "edited" and rec["repack"]["sidecars_left"] == [f"{name}: {ASS}"], rec["repack"]
    assert rec["repack"]["sidecars_unread"] == {name: ASS} and open(side, "rb").read() == raw
    assert rec["alerts"] == [f"unreadable: Converted the MP4/QuickTime file to MKV. The subtitles in {name} can't go into the MKV as they are, "
                             "because the file holds ASS or SSA text under a .srt name. The conversion to MKV left them out, and the file was "
                             "left beside the video as it is."], rec["alerts"]


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["no bom", "utf-8 bom"])
def test_an_ass_sidecar_in_a_legacy_codepage_gets_the_ass_reason(env, monkeypatch, bom):
    """An ASS script in Windows-1252 with French text has no charset AMG can tell, as it holds no SubRip cue. Its
    section lines are ASCII, so it still gets the ASS reason, in the decision line and in the notice. A UTF-8 BOM
    before the cp1252 text gives the same. mkvmerge 102 identifies both files as SSA/ASS too."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    raw = bom + ("[Script Info]\n" + ASS_BODY.replace("Line", "C'est déjà très bien, garçon, ligne")).encode("cp1252")
    side = mkv[:-4] + ".fr.srt"
    with open(side, "wb") as f:
        f.write(raw)
    (s,) = hook.sidecar_subs(mp4, unknown=True)
    assert (s["charset"], s["misread"]) == (None, ASS), s
    hook.main([])
    rec, name = decided(env), os.path.basename(side)
    assert rec["repack"]["sidecars_left"] == [f"{name}: {ASS}"] and rec["repack"]["sidecars_unread"] == {name: ASS}, rec["repack"]
    assert open(side, "rb").read() == raw and "because the file holds ASS or SSA text under a .srt name" in rec["alerts"][0], rec["alerts"]


def test_the_notice_names_ass_sidecars_beside_others():
    """Several sidecars left out share the two sentences, and the ASS ones get a sentence of their own."""
    f = lambda tracks: {"kind": "unreadable", "container": "AVI", "tracks": {n: {"unread": why} for n, why in tracks.items()}}
    head = ("Converted the AVI file to MKV. The subtitles in 3 files can't go into the MKV as they are. The conversion to MKV left them out, and "
            "the files were left beside the video as they are. ")
    text = lambda tracks: hook.FINDINGS["unreadable"][1](f(tracks), None).replace(hook.B, "").replace(hook.E, "")   # no bold marks
    assert text({"a.1.srt": ASS, "a.2.srt": "mkvmerge drops 2 of its cues", "a.3.srt": True}) == head + (
        "The MKV would lose some of the lines of a.2.srt or show them wrong. The file a.1.srt holds ASS or SSA text under a .srt name. AMG "
        "can't tell the character set of a.3.srt.")
    assert text({"a.1.srt": ASS, "a.2.srt": ASS, "a.3.srt": ASS}) == head + (
        "The files a.1.srt, a.2.srt and a.3.srt hold ASS or SSA text under a .srt name.")
    assert hook.title(f({"a.1.srt": ASS}))[0] == "Subtitles left out of the MKV"
