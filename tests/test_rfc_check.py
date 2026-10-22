# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The RFC 9559 check of the Matroska elements outside the Clusters (docs/design.md, "RFC 9559 check"). Each plant is
one edit of a copy of the layouts fixture's mkvmerge file, made-up media with chapters, tags and five tracks."""
import json
import os
import sys

import pytest

from test_arr_media_guard import el, env, hook, layouts, mk, seek_entry  # noqa: F401
from test_header_room import cues_bytes

D = hook.arr_decide
TRACKS, TRACKENTRY, VIDEO, AUDIO, TAGS, UNDEFINED = 0x1654AE6B, 0xAE, 0xE0, 0xE1, 0x1254C367, 0x1254C3FF


def find(b, *path):
    """(start, data start, size) of the first element along path, ids from the Segment down. An item (id, child) takes
    the first element id that holds an element child, as the TrackEntry of the audio. An element of unknown size ends
    at the end of its parent (RFC 8794 6.2)."""
    ds, seg = D.segment_start(bytes(b[:64]))
    p, end = ds, ds + seg
    for want in path:
        want, inner = want if isinstance(want, tuple) else (want, None)
        while (e := D.element(b, p))[0] != want or (inner and not any(c[0] == inner for c in D.children(b, e[1], e[1] + e[2]))):
            p = e[1] + e[2]
            assert p < end, (hex(want), path)
        start, p, end = p, e[1], end if e[2] is None else e[1] + e[2]
    return start, e[1], e[2]


def version(b):   # DocTypeVersion 3 in a file with elements of version 4
    e = D.element(b, 0)
    (_, d, n), = [c for c in D.children(b, e[1], e[1] + e[2]) if c[0] == 0x4287]
    b[d + n - 1] = 3


def rate(b):   # SamplingFrequency 0
    _, d, n = find(b, TRACKS, (TRACKENTRY, AUDIO), AUDIO, 0xB5)
    b[d:d + n] = bytes(n)


def width(b):   # DisplayWidth 0
    _, d, n = find(b, TRACKS, (TRACKENTRY, VIDEO), VIDEO, 0x54B0)
    b[d:d + n] = bytes(n)


def muxer(b):   # MuxingApp written with the id 0x80, as an old muxer did: the same bytes with a 2-byte size
    p, d, n = find(b, D.INFO, 0x4D80)
    assert d - p == 3, d - p
    b[p:d] = bytes([0x80, 0x40, n])


def twice(b):   # WritingApp written with the id of MuxingApp: two MuxingApp elements, and no WritingApp
    p, d, n = find(b, D.INFO, 0x5741)
    b[p:p + 2] = (0x4D80).to_bytes(2, "big")


def overrun(b):   # a TagString whose size runs past its SimpleTag
    p, d, n = find(b, TAGS, 0x7373, 0x67C8, 0x4487)
    w = d - p - 2
    b[p + 2:d] = ((0x80 >> (w - 1)) << 8 * (w - 1) | (1 << 7 * w) - 2).to_bytes(w, "big")


def unknown(b):   # DateUTC under an id that RFC 9559 does not define
    p, d, n = find(b, D.INFO, 0x4461)
    b[p + 1] = 0x62


def live(b, want=D.CLUSTER):   # the first element want with the unknown size of a live stream. RFC 9559 allows it a Cluster.
    p, d, n = find(b, want)
    w = d - p - 4
    b[p + 4:d] = ((0x80 >> (w - 1)) << 8 * (w - 1) | (1 << 7 * w) - 1).to_bytes(w, "big")


def undefined(b):   # the Tags and its SeekID in the front SeekHead under a level-1 id that RFC 9559 does not define
    for p in (b.index(TAGS.to_bytes(4, "big")), find(b, TAGS)[0]):
        b[p:p + 4] = UNDEFINED.to_bytes(4, "big")


def nal(b):   # the NAL length of a video block in the last Cluster made 100 bytes too large: payload damage
    ds, seg = D.segment_start(bytes(b[:64]))
    last = [e for e in D.children(b, ds, ds + seg) if e[0] == D.CLUSTER][-1]
    blocks = [e for e in D.children(b, last[1], last[1] + last[2]) if e[0] == D.SIMPLEBLOCK and b[e[1]] == 0x81]   # track 1, the video
    d = blocks[len(blocks) // 2][1] + 4   # the track, the timecode and the flags, then the 4-byte NAL length
    b[d:d + 4] = (int.from_bytes(b[d:d + 4], "big") + 100).to_bytes(4, "big")


def seekhead_last(b, kinds=(D.CLUSTER,)):
    """A SeekHead of every Cluster as the last element of the Segment, as libmkv wrote it, and one zero byte past
    the Segment end. kinds names the level-1 elements it lists."""
    ds, seg = D.segment_start(bytes(b[:64]))
    head = el(D.SEEKHEAD, b"".join(seek_entry(i, d - ds - 12) for i, d, n in D.children(b, ds, ds + seg) if i in kinds))
    assert b[ds - 8] == 1   # mkvmerge writes an 8-byte Segment size
    b[ds - 8:ds] = (1 << 56 | seg + len(head)).to_bytes(8, "big")
    b[ds + seg:] = head + b"\0"


# mkvmerge writes EditionFlagHidden and ChapterFlagEnabled, which RFC 9559 dropped, so the chapters of src give these
NOTES = ["Chapters/EditionEntry: element 0x45bd, which RFC 9559 does not define",
         "Chapters/EditionEntry/ChapterAtom: element 0x4598, which RFC 9559 does not define"]
TAIL_ERRORS = "the 1 bytes past the Segment end make ffmpeg read the SeekHead before them again, and it prints errors there"
PLANTS = {
    "version": (version, {"version": ["EBML header: DocTypeVersion 3, and the file uses {late} of version 4 (RFC 9559 7)"]}),
    "rate": (rate, {"range": ["Tracks/TrackEntry/Audio/SamplingFrequency is 0, and RFC 9559 wants over 0"]}),
    "width": (width, {"range": ["Tracks/TrackEntry/Video/DisplayWidth is 0, and RFC 9559 wants 1 or more"]}),
    "muxer": (muxer, {"schema": ["Info: ChapterDisplay (0x80) sits there, and its place is Chapters/EditionEntry/ChapterAtom (RFC 9559 5.1)",
                                 "Info: no MuxingApp, which is mandatory (RFC 9559 5.1)"]}),
    "twice": (twice, {"schema": ["Info: 2 MuxingApp elements, where RFC 9559 allows 1", "Info: no WritingApp, which is mandatory (RFC 9559 5.1)"]}),
    "overrun": (overrun, {"framing": ["Tags/Tag/SimpleTag: the element at byte {at} runs past the end of its parent (RFC 8794 6)"]}),
    "tags_size": (lambda b: live(b, TAGS), {"framing": ["Segment: the Tags at byte {at} has an unknown size (RFC 8794 6.2)"]}),
    "seekhead_last": (seekhead_last, {"tail_errors": [TAIL_ERRORS], "padding": ["1 bytes past the Segment end"]}),
    "seekhead_cues": (lambda b: seekhead_last(b, (D.CUES, TAGS)), {"tail_errors": [TAIL_ERRORS], "padding": ["1 bytes past the Segment end"]}),
}


# Payload damage beside the tail fault. mkvmerge copies it with no warning, so no header repair ends it.
DAMAGED = {"nal_sh": lambda b: (nal(b), seekhead_last(b)), "nal_shc": lambda b: (nal(b), seekhead_last(b, (D.CUES, TAGS)))}


def planted(layouts, tmp_path, name):
    """A copy of the layouts file src with the plant name, as (path, its mkvmerge probe)."""
    b = bytearray(layouts["src"].read_bytes())
    {**{k: v[0] for k, v in PLANTS.items()}, **DAMAGED, "unknown": unknown, "undefined": undefined, "live": live}[name](b)
    path = tmp_path / "Movie (2000)" / f"{name}.mkv"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b)
    return str(path), hook.mkvmerge(str(path))


@pytest.mark.parametrize("name", PLANTS)
def test_each_rfc_9559_fault_is_a_header_issue(layouts, tmp_path, name):
    """Owner 2026-10-20: every fault the check finds is a header issue, and the run that finds it repairs it. Most
    faults name no byte, so a remux that writes one again names it the same way. A framing fault and a SeekHead fault
    name a byte."""
    path, j = planted(layouts, tmp_path, name)
    hp, b = hook.header_of(path, j), bytearray(open(path, "rb").read())
    late = hp["rfc"].get("version", ["of"])[0].partition("uses ")[2].partition(" of")[0]   # the mkvmerge version decides the list
    at = find(b, TAGS)[0] if name == "tags_size" else find(b, TAGS, 0x7373, 0x67C8, 0x4487)[0]   # the byte a framing fault names
    want = {k: [x.format(at=at, late=late) for x in v] for k, v in PLANTS[name][1].items()}
    assert hp["rfc"] == want and hp["notes"] == NOTES and (name != "version" or "LanguageBCP47" in late.split(", ")), hp["rfc"]
    found = {k: v for k, v in want.items() if k in sys.modules[f"{hook._name}.matroska"].KINDS}
    assert hp["issue"] == ["the file holds 1 bytes past the end of its Matroska Segment"] * name.startswith("seekhead") + [hook.rfc_issue(found)], hp["issue"]
    assert not hook.padding_only(hp) and hp["first"] is False and hp["video"] == 30.0


def test_an_element_rfc_9559_does_not_define_is_a_note(layouts, tmp_path):
    """A reader skips an element it does not know (RFC 9559 7), so it is a note, never an issue, and it fails no
    repair. The common case still reads no stream end. The front SeekHead may list such an element at level 1. The
    check then reads only its header."""
    path, j = planted(layouts, tmp_path, "unknown")
    hp = hook.header_of(path, j)
    assert (hp["rfc"], hp["issue"], hp["notes"], hp["end"]) == ({}, [], ["Info: element 0x4462, which RFC 9559 does not define"] + NOTES, None), hp
    path, j = planted(layouts, tmp_path, "undefined")
    hp = hook.header_of(path, j)
    assert (hp["rfc"], hp["issue"], hp["notes"]) == ({}, [], NOTES + ["Segment: element 0x1254c3ff, which RFC 9559 does not define"]), hp
    assert hp["read"] <= hook.HEADER_READ + cues_bytes(path) + 2 * 24, hp["read"]   # 24 bytes for the header of the Cues and of the element, as outside() counts


def test_a_live_cluster_of_unknown_size_is_no_fault(layouts, tmp_path):
    """RFC 9559 lets a Cluster have an unknown size, as a live stream writes it. The walk of the front ends at it, and
    the check finds no fault. An earlier build called it a framing fault."""
    path, j = planted(layouts, tmp_path, "live")
    assert hook.header_of(path, j)["rfc"] == {}


@pytest.mark.parametrize("name", PLANTS)
def test_the_header_repair_with_mkvmerge_ends_each_fault(layouts, tmp_path, settings, name):
    """mkvmerge 102 writes each element again from what it read: the DocTypeVersion of the elements it writes, its own
    MuxingApp, the frequency and display size of the stream, and the tags whole. A SeekHead of Clusters goes with the
    byte past the Segment end. The proof holds every track, and the new file has no fault. Its notes are those of any
    mkvmerge file with chapters."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    path, j = planted(layouts, tmp_path, name)
    hp = hook.header_of(path, j)
    got, result, info = hook.repack(path, j, os.stat(path), True, dict(hp, windows_clean=True))
    assert (got, info.get("rfc_left")) == ("tail_removed" if name.startswith("seekhead") else "header_repaired", None), (result, info)
    after = hook.header_of(path)
    assert (after["rfc"], after["notes"], after["issue"]) == ({}, NOTES, []), after


def test_a_fault_mkvmerge_writes_again_fails_no_repair():
    """A remux that keeps a fault of its original fails no repair, and info names it. A new fault fails it."""
    old = {"rfc": {"range": ["Tracks/TrackEntry/Video/DisplayWidth is 0, and RFC 9559 wants 1 or more"], "padding": ["9 bytes past the Segment end"]}}
    assert hook.rfc_again({"rfc": {"range": old["rfc"]["range"]}}, old) == old["rfc"]["range"]
    assert hook.rfc_again({"rfc": {"schema": ["Info: no MuxingApp, which is mandatory (RFC 9559 5.1)"]}}, old) == []
    assert hook.rfc_again({"rfc": {}}, old) == [] and hook.rfc_again(None, old) == []
    both = {"rfc": {"range": old["rfc"]["range"], "schema": ["Info: no MuxingApp, which is mandatory (RFC 9559 5.1)"]}}
    assert hook.rfc_again(both, old) == []   # one fault is new, so the repair fails on it


def test_the_repair_refuses_a_new_file_with_an_rfc_fault_its_original_lacks(tmp_path, monkeypatch):
    """remux.header_checks() fails a repair whose new file has an RFC 9559 fault that the original did not hold. A
    fault the original held too passes that step and is named in rfc_left, and the next step, the size, fails here."""
    tmp = tmp_path / "new.mkv"
    tmp.write_bytes(bytes(1000))
    fault = {"schema": ["Info: no MuxingApp, which is mandatory (RFC 9559 5.1)"]}
    monkeypatch.setattr(hook, "header_probe", lambda path, j: {"cues": True, "rfc": fault, "issue": [hook.rfc_issue(fault)]})
    monkeypatch.setattr(hook, "stored_places", lambda path, streams: {})   # the zero bytes hold no frame to read
    monkeypatch.setattr(hook, "ff_streams", lambda path: ("matroska,webm", [], 30.0))
    monkeypatch.setattr(hook, "tail_ends", lambda f, stop, durations, tracks: ({1: 30000}, b"", 0))   # the streams end at 30 s
    new, hp, info = {"container": {"properties": {"duration": 30 * 10**9}}, "tracks": []}, {"expect": 30.0, "video": 30.0, "rfc": {}}, {}
    assert hook.header_checks(str(tmp), new, 10**9, hp, info, str(tmp_path)) == "the new file still has a header issue: " + hook.rfc_issue(fault)
    assert "rfc_left" not in info
    got = hook.header_checks(str(tmp), new, 10**9, dict(hp, rfc=fault), info, str(tmp_path))
    assert got == "the size changed from 1000000000 to 1000 bytes" and info["rfc_left"] == fault["schema"], got


@pytest.mark.parametrize("name", ["seekhead_last", "seekhead_cues"])
def test_read_pass_errors_past_the_last_cluster_with_a_tail_are_the_tail_fault(layouts, tmp_path, name):
    """The byte past the Segment end makes ffmpeg read the last SeekHead again. It prints an error at each Cluster id
    there, at its place past the last Cluster. A SeekHead of Cues and Tags gives "Truncating packet" lines that name no
    place. 2.18.4 posted "Video may be broken" for both. They are the tail fault, which the header repair removes. The
    same errors in a file with no tail stay a doubt."""
    path, _ = planted(layouts, tmp_path, name)
    certain, zero, more, fields = hook.whole_check(path)
    ff = fields["ffmpeg"]
    assert (certain, zero, more, ff["tail"], ff["tail_errors"]) == (None, [], [], 1, True) and ff["lines"] >= 1, ff
    assert set(ff.get("when") or ["end"]) == {"end"} and (name == "seekhead_cues") == (ff["loose"] == ff["lines"]), ff
    doubt = {"seekhead_last": "a full read of the file found damaged data after the video and audio end",
             "seekhead_cues": f"a full read of the file found {ff['lines']} error{'s' * (ff['lines'] > 1)}"}[name]
    assert D.whole_verdict([], fields["size"], dict(ff, tail=None, tail_errors=False))[2] == [doubt]


@pytest.mark.parametrize("name", ["nal_sh", "nal_shc"])
def test_payload_damage_beside_the_tail_fault_stays_a_doubt(layouts, tmp_path, settings, name):
    """A NAL length too large in the last Cluster prints lines that name no place, as "Invalid NAL unit size". Beside
    the tail fault only the "Truncating packet" lines of the tail go. The damage stays a doubt and posts, with no note
    of the tail. An earlier build dropped every line that names no place when the check found tail_errors."""
    path, _ = planted(layouts, tmp_path, name)
    certain, zero, more, fields = hook.whole_check(path)
    ff = fields["ffmpeg"]
    n = ff["loose"] - ff["truncated"]
    assert ff["tail_errors"] and n >= 1 and (ff["truncated"] >= 1) == (name == "nal_shc"), ff
    assert (certain, zero, more) == (None, [], [f"a full read of the file found {n} error{'s' * (n > 1)}"]), ff
    rec = hook.whole_act(None, {"app": "radarr", "path": path, "grab": {"check": True}}, {"id": "x"}, [], post=False)
    assert rec["outcome"] == "video_doubt" and "note" not in rec, rec


def test_the_tail_fault_takes_only_what_the_tail_makes():
    """The rules of decide.whole_verdict() on made-up reads. A place inside the streams stays a doubt beside the places
    past the last Cluster. Bytes past the Segment end clear "Truncating packet" lines only when the check found
    tail_errors."""
    size = 1000
    read = lambda **k: dict({"ran": True, "lines": 2, "pos": [], "at": [], "loose": 0, "truncated": 0}, **k)
    assert D.whole_verdict([], size, read(pos=[500, 990], at=[0.5, 0.99], when=[100.0, "end"], tail=1))[2] == [
        "a full read of the file found damaged data at 1:40 and after the video and audio end"]
    assert D.whole_verdict([], size, read(pos=[990], at=[0.99], when=["end"], lines=1, tail=1))[2] == []
    assert D.whole_verdict([], size, read(lines=1, loose=1, truncated=1, tail=1, tail_errors=False))[2] == ["a full read of the file found 1 error"]
    assert D.whole_verdict([], size, read(lines=3, loose=3, truncated=2, tail=1, tail_errors=True))[2] == ["a full read of the file found 1 error"]
    assert D.whole_verdict([], size, read(lines=2, loose=2, truncated=2, tail=1, tail_errors=True))[2] == []


def test_the_whole_file_check_logs_the_tail_fault_and_posts_nothing(layouts, tmp_path, settings):
    """The whole-file check of such a file ends video_checked, and its note names the tail."""
    path, _ = planted(layouts, tmp_path, "seekhead_last")
    rec = hook.whole_act(None, {"app": "radarr", "path": path, "grab": {"check": True}}, {"id": "x"}, [], post=False)
    assert rec["outcome"] == "video_checked" and "findings" not in rec, rec
    assert rec["note"] == "the read pass errors after the last Cluster come from the 1 bytes past the Segment end, which the header repair removes"


def test_the_library_pass_leaves_padding_alone(layouts, tmp_path):
    """Owner 2026-10-20: the --check-video pass lists the real header faults, and --only-header repairs them. A file
    whose only fault is padding is logged and left alone there. A run on one file repairs padding too."""
    hp = {"rfc": {"padding": ["9 bytes past the Segment end"]}, "issue": ["the file holds 9 bytes past the end of its Matroska Segment"]}
    assert hook.padding_only(hp)
    assert not hook.padding_only(dict(hp, issue=hp["issue"] + ["no usable Cues index: no SeekHead lists the Cues"]))
    assert not hook.padding_only(dict(hp, rfc=dict(hp["rfc"], tail_errors=[TAIL_ERRORS])))
    assert not hook.padding_only(None) and not hook.padding_only({"rfc": {}, "issue": []})


def test_the_audit_counts_the_fault_kinds_of_each_file(env, tmp_path, capsys):
    """--audit counts the files with each fault kind, by the latest line of each file that ran the header check. A
    later clean check clears a file, and so does a repair in the run that found the fault. A file that is gone counts
    no more."""
    paths = [tmp_path / f"m{i}.mkv" for i in range(4)]
    for p in paths:
        p.write_bytes(b"x")
    line = lambda p, rfc, **more: hook.decision(dict(app="radarr", source="video_scan", apply=False, path=str(p), outcome="video_checked", result="ok",
                                                     rfc=rfc, **more), 0)
    line(paths[0], {"version": ["a"], "padding": ["9 bytes past the Segment end"]})
    line(paths[1], {"range": ["b"]})
    line(paths[1], {})   # repaired since
    line(paths[2], {"version": ["c"]})
    os.remove(paths[2])
    line(paths[3], {"range": ["d"]}, header_repair={"code": "header_repaired"})   # the run that found it repaired it
    hook.main(["--audit", "radarr", "--since", "24h"])
    assert "RFC 9559 faults:\npadding 1, version 1" in capsys.readouterr().out
    assert " rfc_padding=1 rfc_version=1" in env["syslog"][-1]


def test_the_video_scan_lists_real_header_faults_and_logs_padding(layouts, tmp_path, settings, monkeypatch):
    """The --check-video pass lists a file with a real header fault, and a file whose only fault is padding gets its
    decision line only. Both lines hold the fault kinds, which --audit counts. The scan writes nothing."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    rate_path, _ = planted(layouts, tmp_path, "rate")
    pad = tmp_path / "Movie (2000)" / "pad.mkv"
    pad.write_bytes(layouts["src"].read_bytes() + bytes(9))
    files = {rate_path: os.path.getsize(rate_path), str(pad): os.path.getsize(pad)}
    monkeypatch.setattr(hook, "library", lambda app, ids: [(1, rate_path, "Rate (2000)", "eng", 0, False), (2, str(pad), "Pad (2000)", "eng", 0, False)])
    monkeypatch.setattr(hook, "SCAN_PACE", 0)
    hook.main(["--backfill", "radarr", "--check-video", "--workers", "1"])
    listed = open(os.path.join(str(tmp_path), "video-scan-radarr.txt")).read().splitlines()
    assert [x.split("\t")[:2] for x in listed] == [["HEADER", "Rate (2000)"]], listed
    lines = [json.loads(x) for x in open(tmp_path / "log.jsonl") if '"video_scan"' in x]
    assert [r["rfc"] for r in lines] == [PLANTS["rate"][1], {"padding": ["9 bytes past the Segment end"]}], lines
    assert {p: os.path.getsize(p) for p in files} == files


def test_a_plan_marks_a_repair_of_padding_alone(env, monkeypatch, tmp_path):
    """A dry run with --plan-out marks the header repair of a file whose only fault is padding, so --only-header leaves
    it. A repair of a real fault carries no mark."""
    import test_write_stage as ws
    env["probe"] = {"container": {"properties": {"duration": 7200 * 10**9}},   # English plays first, so no flag edit
                    "tracks": [mk("video", "und", 1, True), mk("audio", "eng", 2, True, audio_channels=6), mk("subtitles", "eng", 3, False)]}
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 70, "movieId": 7, "path": env["path"],
                                                                              "mediaInfo": {"audioStreamCount": 2}})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    plans = tmp_path / "plans.jsonl"
    for hp, mark in ((ws.PADDED, True), (dict(ws.PADDED, rfc=dict(ws.PADDED["rfc"], tail_errors=[TAIL_ERRORS])), None)):
        ws.header_issue(env, monkeypatch, **hp)
        hook.main(["--backfill", "radarr", "--plan-out", str(plans)])
        row = json.loads(plans.read_text())
        assert (row["header_repair"], row.get("padding_only")) == ("would_repair_header", mark), row
