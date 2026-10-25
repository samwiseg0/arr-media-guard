# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""A remux that carries the flag and language edits of a plan as mkvmerge track options, see remux.merge_argv(), and
its proof against the planned state, see remux.tracks_fault(). Also the resync that repack() accepts at the zero run
mkvpropedit encloses in a Segment, see remux.resynced(). Every file is made up."""
import json
import os
import shutil
import types

import pytest

import amg
from test_arr_media_guard import hook, REAL_RUN, REAL_MKVMERGE, lace_mkvs, mkvs, state_dir_in_tmp  # noqa: F401
decide = hook.arr_decide

ZEROS = 40000   # the zero bytes past the Segment end of a *_zp file, 6 percent of its size, over REPACK_SIZE


@pytest.fixture(scope="module")
def zp(tmp_path_factory):
    """Made-up 30 s files with H.264 video, AAC audio and two SubRip tracks: src.mkv from mkvmerge, its first subtitle
    forced, its second es-419 and not default, ff.mkv ffmpeg's copy of it, and ff_zp.mkv and mm_zp.mkv, ff.mkv and
    src.mkv with ZEROS zero bytes past the Segment end. ff_tail.mkv is ff.mkv with 2 MiB of zero bytes past it, over
    TAIL_MIN. ws.mkv holds a SubRip track whose lines end in spaces and a tab. ff_zp_ed.mkv is ff_zp.mkv after an mkvpropedit edit that grows
    its Tracks. mkvpropedit encloses the zeros in the Segment and writes Tracks after them."""
    d = tmp_path_factory.mktemp("zp")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=30:size=160x90:rate=25", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=30", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", str(d / "av.mkv")], check=True)
    (d / "s.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:20,000 --> 00:00:21,000\nAgain\n")
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "src.mkv"), str(d / "av.mkv"), "--language", "0:en", "--forced-display-flag", "0:1", str(d / "s.srt"),
              "--language", "0:es-419", "--default-track-flag", "0:0", str(d / "s.srt")], check=True)
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-i", str(d / "src.mkv"), "-map", "0", "-c", "copy", str(d / "ff.mkv")], check=True)
    for name, base, n in (("ff_zp", "ff.mkv", ZEROS), ("mm_zp", "src.mkv", ZEROS), ("ff_tail", "ff.mkv", 2 << 20)):
        (d / f"{name}.mkv").write_bytes((d / base).read_bytes() + bytes(n))
    (d / "ws.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello   \n\n2\n00:00:20,000 --> 00:00:21,000\nAgain\t\n")
    REAL_RUN(["mkvmerge", "-q", *hook.KEEP_PACKETS, "-o", str(d / "ws.mkv"), str(d / "av.mkv"), str(d / "ws.srt")], check=True)
    shutil.copy(d / "ff_zp.mkv", d / "ff_zp_ed.mkv")
    REAL_RUN(["mkvpropedit", str(d / "ff_zp_ed.mkv"), *hook.propedit_args(edits_of(REAL_MKVMERGE(str(d / "ff_zp.mkv"))), 1)], check=True)
    return d


def edits_of(j):
    """Six edits of a plan for the files of zp: the Original language flag of the audio on, the default and forced
    flags of the first subtitle off, the second subtitle es-MX, with its BCP 47 edit, and default."""
    a1, s1, s2 = decide.classify(j)
    return [[a1["sel"], 1, a1["original"], decide.ORIGINAL_FLAG], [s1["sel"], 0, 1], [s1["sel"], 0, 1, decide.FORCED_FLAG],
            [s2["sel"], "es-MX", "spa", decide.LANG_EDIT], [s2["sel"], "es-MX", "es-419", decide.LANG_IETF], [s2["sel"], 1, 0]]


def copy(zp, tmp_path, name):
    """A copy of the file name of zp, with its probe."""
    path = str(tmp_path / name)
    shutil.copy(zp / name, path)
    return path, REAL_MKVMERGE(path)


def header(path, j, **kw):
    """The header stage of a made-up header issue of path, as the existing repair tests make it, with kw."""
    hp = hook.header_of(path, j)
    return dict(hp, issue=["a made-up issue"], expect=hp["duration"], video=hp["duration"], streams=hp["duration"], windows_clean=True, **kw)


def runs(monkeypatch):
    """The argv of each subprocess.run() call from now on."""
    seen, real = [], hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, *a, **k: seen.append(list(argv)) or real(argv, *a, **k))
    return seen


@pytest.mark.parametrize("name", ["ff_zp.mkv", "mm_zp.mkv", "ff.mkv"])
def test_a_header_repair_carries_the_edits_of_the_plan(zp, tmp_path, settings, monkeypatch, name):
    """A header repair with six edits sets each one in the same mkvmerge run, with no mkvpropedit. The new file shows
    every edit, holds no layout fault and no bytes past the Segment end, and keeps every other property."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, name)
    edits, tail = edits_of(j), ZEROS if "_zp" in name else 0
    seen = runs(monkeypatch)
    code, result, info = hook.repack(path, j, os.stat(path), True, header(path, j, edit_tail=tail), edits)
    assert result == ("tail removed" if tail else "header repaired"), (result, info)
    new = REAL_MKVMERGE(path)
    assert decide.unapplied(new, edits) == [] and hook.layout_of(path) is None and hook.segment_short(path) == 0
    assert not [a for a in seen if "mkvpropedit" in a] and len([a for a in seen if "mkvmerge" in a and "-o" in a]) == 1
    s2 = new["tracks"][3]["properties"]
    assert (s2["language_ietf"], s2["default_track"], new["tracks"][2]["properties"]["forced_track"]) == ("es-MX", True, False)
    assert hook.tracks_fault(j, new, edits) is None and hook.tracks_fault(j, new) == "track 1 changed its flag_original"


def test_a_subtitle_remux_carries_the_edits_and_repairs_the_header_in_one_write(zp, tmp_path, settings, monkeypatch):
    """A retime of the first subtitle, six edits and the zero tail of hp go into one mkvmerge run. The new file shows
    the retime, every edit and no tail, and passes the header checks of a header repair."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "ff_zp.mkv")
    edits, hp = edits_of(j), header(path, j, edit_tail=ZEROS)
    seen = runs(monkeypatch)
    code, result, info = hook.resub(path, j, os.stat(path), True, {2: {"rate": "1/1", "offset": 0.5}}, edits=edits, hp=hp)
    assert code == "subtitles_remuxed" and all(e["match"] for e in info["proof"]), (result, info)
    writes = [a for a in seen if "-o" in a and a[a.index("-o") + 1] == hook.repack_tmp(path)]
    assert len(writes) == 1 and not [a for a in seen if "mkvpropedit" in a and path in a], writes
    new = REAL_MKVMERGE(path)
    assert decide.unapplied(new, edits) == [] and hook.segment_short(path) == 0 and info["new_cues"] is True
    assert "repair the header: a made-up issue" in hook.resub(path, new, os.stat(path), False, {2: {"rate": "1/1", "offset": 0.5}}, hp=hp)[1]


def test_a_subtitle_remux_that_repairs_a_header_pairs_each_lag_less_the_dropped_track(zp, tmp_path, settings, monkeypatch):
    """The remux drops subtitle track 2 and repairs the header. The new file's subtitle stream 2 is track 3, so the
    header checks take the lag of track 3 in the original, see remux.paired_lags(). The lags of the original are made
    up, one per track."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "ff_zp.mkv")
    real = hook.original_frames
    monkeypatch.setattr(hook, "original_frames", lambda src: (*real(src)[:2], dict(real(src)[2], subtitle=[7.0, 0.25])))
    code, result, info = hook.resub(path, j, os.stat(path), True, {3: {"rate": "1/1", "offset": 0.5}}, drop=[2], hp=header(path, j, edit_tail=ZEROS))
    assert code == "subtitles_remuxed" and info["lag"][2][0] == 0.25 and sorted(info["lag"]) == [0, 1, 2], (result, info.get("lag"))


def test_a_track_from_its_own_file_takes_its_edits_from_track_args(zp, tmp_path, settings, monkeypatch):
    """The retimed subtitle comes from its own file. Its language and default flag edits reach it through track_args()
    of its planned properties, and its track id gets no option. The copied subtitle gets its edit as an option."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "src.mkv")
    a1, s1, s2 = decide.classify(j)
    edits = [[s2["sel"], "es-MX", "spa", decide.LANG_EDIT], [s2["sel"], 1, 0], [s1["sel"], 0, 1]]
    seen = runs(monkeypatch)
    code, result, info = hook.resub(path, j, os.stat(path), True, {3: {"rate": "1/1", "offset": 0.5}}, edits=edits)
    assert code == "subtitles_remuxed", (result, info)
    argv = next(a for a in seen if "-o" in a and a[a.index("-o") + 1] == hook.repack_tmp(path))
    own = argv[len(argv) - argv[::-1].index("--language") - 1:]   # the options of the last input, the retimed track's file
    assert own[1] == "0:es-MX" and own[own.index("--default-track-flag") + 1] == "0:1", own
    assert not [x for x in argv if x.startswith("3:")] and argv.count("2:0") == 1 and "--default-track-flag" == argv[argv.index("2:0") - 1], argv
    new = REAL_MKVMERGE(path)
    assert decide.unapplied(new, edits) == [] and new["tracks"][3]["properties"]["uid"] == j["tracks"][3]["properties"]["uid"]


@pytest.mark.parametrize("drop, why", [(("--original-flag", "1:1"), "track 1 changed its flag_original"),
                                       (("--language", "3:es-MX"), "the new file does not show the edits")], ids=["flag", "language"])
def test_a_remux_that_misses_an_edit_fails_and_the_original_stays(zp, tmp_path, settings, monkeypatch, drop, why):
    """An argv that leaves one edit out writes a file the proof refuses: a flag by its planned property, a language by
    decide.unapplied(). The original stays byte for byte."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "src.mkv")
    before, real = open(path, "rb").read(), hook.merge_argv

    def short(*a, **k):
        argv = real(*a, **k)
        at = next(n for n in range(len(argv) - 1) if tuple(argv[n:n + 2]) == drop)
        return argv[:at] + argv[at + 2:]
    monkeypatch.setattr(hook, "merge_argv", short)
    code, result, info = hook.repack(path, j, os.stat(path), True, header(path, j), edits_of(j))
    assert code == "header_repair_failed" and why in result and open(path, "rb").read() == before, result
    assert not os.path.exists(hook.repack_tmp(path))


def test_an_edit_of_a_track_the_remux_removes_stops_it(zp, tmp_path, settings):
    """An edit of a subtitle track the remux drops cannot land, so the remux stops before mkvmerge runs."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "src.mkv")
    before = open(path, "rb").read()
    code, result, _ = hook.resub(path, j, os.stat(path), True, {}, (3,), edits=edits_of(j))
    assert code == "subtitle_remux_failed" and "an edit names track 3, which the remux removes" in result and open(path, "rb").read() == before


@pytest.mark.parametrize("change, ok", [({}, True), ({"enclosed": None}, False), ({"enclosed": "after"}, False), ({"streams": 10.0}, False)],
                         ids=["at the run", "no run", "run after the position", "streams end earlier"])
def test_a_header_repair_accepts_the_resync_at_enclosed_zeros(zp, tmp_path, settings, change, ok):
    """mkvpropedit grew the Tracks of ff_zp.mkv and enclosed its zero tail in the Segment. mkvmerge then warns that it
    resyncs at the zeros, and finds no element after them. A repair accepts those warnings only at hp["enclosed"] or
    after it, and only when the last timestamp read is the end of the streams. The new file keeps every frame and
    every edit of the file, and its header has no issue."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "ff_zp_ed.mkv")
    at, data = os.path.getsize(zp / "ff.mkv"), open(path, "rb").read()   # the first zero byte, where the old Segment ended
    run = len(data[at:]) - len(data[at:].lstrip(b"\0"))
    assert run == ZEROS
    hp = header(path, j, enclosed=at, enclosed_size=run)
    hp.update({k: at + 1 if v == "after" else v for k, v in change.items()})
    before = open(path, "rb").read()
    code, result, info = hook.repack(path, j, os.stat(path), True, hp)
    assert "Resync failed: no valid Matroska level 1 element found." in info["warnings"], info
    if not ok:   # the resync guard refuses it, before any check of the new file
        assert code == "header_repair_failed" and result.startswith("header repair failed: mkvmerge exited 1: ") and open(path, "rb").read() == before, result
        return
    assert result == "header repaired" and info["new_cues"] is True, (result, info)
    new = REAL_MKVMERGE(path)
    assert hook.tracks_fault(j, new) is None and hook.header_of(path, new)["issue"] == [] and hook.layout_of(path) is None
    assert info["frames"] == 750 and hook.segment_short(path) == 0


def test_resynced_takes_only_the_three_lines_of_each_input():
    """The three lines of each input pass, at or after hp["enclosed"] and with the last timestamp at the end of the
    streams. Any other line, a missing line, or a position before the run fails."""
    lines = ["Warning: /m/f.mkv: Error in the Matroska file structure at position 680566. Resyncing to the next level 1 element.",
             "Warning: The last timestamp processed before the error was encountered was 00:00:30.022000000.",
             "Warning: Resync failed: no valid Matroska level 1 element found."]
    hp = {"enclosed": 680566, "streams": 30.0}
    assert hook.resynced("\n".join(lines * 2), hp) and hook.resynced("\n".join(lines), hp)
    assert not hook.resynced("\n".join(lines[:2]), hp) and not hook.resynced("\n".join(lines + ["Warning: another warning"]), hp)
    assert not hook.resynced("\n".join(lines), dict(hp, enclosed=680567)) and not hook.resynced("\n".join(lines), dict(hp, streams=28.9))
    assert not hook.resynced("\n".join(lines), dict(hp, enclosed=None)) and not hook.resynced("", hp)


def test_the_planned_state_and_the_options_of_each_edit():
    """planned_state() sets each edited property and gives its option. A language edit and its BCP 47 edit share one option.
    An edit of an unknown track, or of a property that decide does not edit, raises."""
    j = {"tracks": [{"id": 0, "type": "video", "properties": {"uid": 1}},
                    {"id": 1, "type": "audio", "properties": {"uid": 2, "language": "eng"}},
                    {"id": 2, "type": "subtitles", "properties": {"uid": 3, "language": "spa", "language_ietf": "es-419", "forced_track": True}}]}
    edits = [["track:=2", 1, None, decide.ORIGINAL_FLAG], ["track:=3", 0, 1, decide.FORCED_FLAG], ["track:=3", "es-MX", "spa", decide.LANG_EDIT],
             ["track:=3", "es-MX", "es-419", decide.LANG_IETF], ["track:=3", 0, 1]]
    want, opts = hook.planned_state(j, edits)
    assert opts == {1: {"--original-flag": 1}, 2: {"--forced-display-flag": 0, "--language": "es-MX", "--default-track-flag": 0}}
    p1, p2 = want["tracks"][1]["properties"], want["tracks"][2]["properties"]
    assert p1["flag_original"] is True
    assert (p2["forced_track"], p2["language_ietf"], p2["default_track"], p2["language"]) == (False, "es-MX", False, "spa")
    assert j["tracks"][2]["properties"]["forced_track"] is True   # j stays as it is
    with pytest.raises(RuntimeError):
        hook.planned_state(j, [["track:=9", 1, 0]])
    for prop in ("flag-unknown", "name", "flag-hearing-impaired", "flag-commentary"):   # decide.unapplied() reads none of these back
        with pytest.raises(RuntimeError):
            hook.planned_state(j, [["track:=2", 1, 0, prop]])


def test_a_removed_track_whose_uid_stays_fails_the_track_check():
    """A remux that drops subtitle track 1 and writes its UID on the track that takes its place fails, though the
    types, codecs and languages match. An edit of track 2, which takes the place of track 1, shows by its selector. A
    track with no UID has a selector by its place, and the edit maps to its new place."""
    t = lambda i, uid, kind="subtitles", **kw: {"id": i, "type": kind, "codec": "SubRip/SRT", "properties": {"uid": uid, "language": "eng", **kw}}
    j = {"tracks": [t(0, 1, "video"), t(1, 2), t(2, 3)]}
    assert hook.tracks_fault(j, {"tracks": [t(0, 1, "video"), t(1, 2)]}, drop=(1,)) == "a removed track is still in the file"
    assert hook.tracks_fault(j, {"tracks": [t(0, 1, "video"), t(1, 3)]}, drop=(1,)) is None
    assert hook.tracks_fault(j, {"tracks": [t(0, 1, "video"), t(1, 3, default_track=True)]}, [["track:=3", 1, 0]], drop=(1,)) is None
    j = {"tracks": [t(0, None, "video"), t(1, None), t(2, None)]}
    assert hook.tracks_fault(j, {"tracks": [t(0, None, "video"), t(1, None, default_track=True)]}, [["track:s2", 1, 0]], drop=(1,)) is None


def test_a_subtitle_remux_that_repairs_a_header_never_laces_again(lace_mkvs, tmp_path, settings):
    """copy.mkv holds ffmpeg's estimates of laced frame times, so a header repair laces its audio again, see
    lace_guess(). A subtitle remux that repairs the header too keeps each frame in its own block, and says why."""
    settings(keep_days=0)
    path = str(tmp_path / "copy.mkv")
    shutil.copy(lace_mkvs / "copy.mkv", path)
    j = REAL_MKVMERGE(path)
    hp = header(path, j)
    assert hook.repack(path, j, os.stat(path), False, hp)[2]["relaced"] == [1]
    code, result, info = hook.resub(path, j, os.stat(path), True, {2: {"rate": "1/1", "offset": 0.5}}, hp=hp)
    assert code == "subtitles_remuxed" and "relaced" not in info, (result, info)
    assert info["relace_skipped"] == "audio stream 1 keeps ffmpeg's estimated times, as a subtitle remux never laces"


def test_a_language_edit_is_proved_by_its_tag_in_the_new_file():
    """A language edit to zsm, a BCP 47 language that decide.track_tag() does not know, reads as the old legacy tag in
    the planned state and as mkvmerge's new legacy tag in the new file. The check takes the language from
    decide.unapplied(), so the edit passes, and a file that lacks the new tag fails."""
    t = lambda legacy, ietf: {"id": 1, "type": "subtitles", "codec": "SubRip/SRT", "properties": {"uid": 7, "language": legacy, "language_ietf": ietf}}
    j, edits = {"tracks": [t("spa", "es-419")]}, [["track:=7", "zsm", "spa", decide.LANG_EDIT]]
    assert hook.tracks_fault(j, {"tracks": [t("may", "zsm")]}, edits) is None
    assert hook.tracks_fault(j, {"tracks": [t("may", "ms")]}, edits).startswith("the new file does not show the edits")


def test_a_tail_that_is_also_an_edit_tail_counts_once(zp, tmp_path, settings):
    """A tail over TAIL_MIN with no Void after Tracks gives hp["tail"] and hp["edit_tail"] the same count. The size
    proof subtracts it once, so the repair of a file of 2.7 MB that holds 2 MiB of it passes."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "ff_tail.mkv")
    code, result, info = hook.repack(path, j, os.stat(path), True, header(path, j, tail=2 << 20, edit_tail=2 << 20))
    assert result == "tail removed" and hook.segment_short(path) == 0 and os.path.getsize(path) < 700000, (result, info)


def test_the_track_check_holds_the_bcp47_tag_and_the_trimmed_events():
    """A remux that changes only the BCP 47 tag of a track fails, though its legacy tag reads the same. A trimmed track
    must hold its events less the dropped ones, and its UID."""
    t = lambda i, uid, kind="subtitles", **p: {"id": i, "type": kind, "codec": "SubRip/SRT", "properties": {"uid": uid, "language": "spa", **p}}
    j = {"container": {"type": "Matroska"}, "tracks": [t(0, 1, "video"), t(1, 2, language_ietf="es-419")]}
    new = {"container": {"type": "Matroska"}, "tracks": [t(0, 1, "video"), t(1, 2, language_ietf="es-ES")]}
    assert hook.tracks_fault(j, new) == "track 1 changed its language_ietf"
    new["tracks"][1] = t(1, 2, language_ietf="es-419", tag_number_of_frames="5")
    info = {"trimmed": {1: {"events": 7, "dropped": 1}}}
    assert hook.header_fault(j, "", new, 0, {"trim": [1]}, info, "") == "the trimmed track 1 holds 5 events, not 6"
    new["tracks"][1]["properties"]["uid"] = 9
    assert hook.header_fault(j, "", new, 0, {"trim": [1]}, info, "") == "track 1 changed its uid"


def test_a_header_repair_carries_the_edits_of_a_trimmed_track(mkvs, tmp_path, settings):
    """A trim writes the SubRip track again from its own Matroska file, which holds the track's UID, see uid_files().
    The track keeps its UID, and its language and default flag edits reach it through track_args()."""
    settings(keep_days=0)
    path = str(tmp_path / "subtitle.mkv")
    shutil.copy(mkvs / "subtitle.mkv", path)
    j = REAL_MKVMERGE(path)
    h = hook.video_check(path)[2]["header"]
    s1 = decide.classify(j)[1]
    edits = [[s1["sel"], "fr-CA", s1["tag"], decide.LANG_EDIT], [s1["sel"], 1 - s1["default"], s1["default"]]]
    code, result, info = hook.repack(path, j, os.stat(path), True, h, edits)
    assert result == "subtitles trimmed" and list(info["trimmed"]) == [2], (result, info)
    new = REAL_MKVMERGE(path)
    assert new["tracks"][2]["properties"]["uid"] == j["tracks"][2]["properties"]["uid"] and decide.unapplied(new, edits) == []
    assert (new["tracks"][2]["properties"]["language_ietf"], new["tracks"][2]["properties"]["default_track"]) == ("fr-CA", bool(1 - s1["default"]))


def test_a_trim_keeps_the_spaces_at_the_ends_of_subtitle_lines(mkvs, tmp_path, settings):
    """A trimmed track goes into the remux through a Matroska file of its own, see uid_files(). Both mkvmerge runs keep
    the spaces and tabs at the ends of its lines, see KEEP_PACKETS."""
    settings(keep_days=0)
    (tmp_path / "ws.srt").write_text("1\n00:00:10,000 --> 00:59:00,000\nHello   \n\n2\n00:00:20,000 --> 00:00:21,000\nAgain\t\n")
    path = str(tmp_path / "ws.mkv")
    REAL_RUN(["mkvmerge", "-q", *hook.KEEP_PACKETS, "-o", path, str(mkvs / "good.mkv"), str(tmp_path / "ws.srt")], check=True)
    j = REAL_MKVMERGE(path)
    code, result, info = hook.repack(path, j, os.stat(path), True, hook.video_check(path)[2]["header"])
    assert result == "subtitles trimmed" and REAL_MKVMERGE(path)["tracks"][2]["properties"]["uid"] == j["tracks"][2]["properties"]["uid"], (result, info)
    REAL_RUN(["mkvextract", path, "tracks", f"2:{tmp_path / 'out.srt'}"], check=True, capture_output=True)
    text = (tmp_path / "out.srt").read_bytes().removeprefix(b"\xef\xbb\xbf")   # mkvextract writes a BOM
    assert text == b"1\n00:00:10,000 --> 00:02:00,023\nHello   \n\n2\n00:00:20,000 --> 00:00:21,000\nAgain\t\n\n", text


def test_a_warning_of_the_mux_of_a_trimmed_track_refuses_the_repair(mkvs, tmp_path, settings, monkeypatch):
    """mkvmerge stops its read of a SubRip file at a number line with no timing line after it, and warns. uid_files()
    refuses that warning, as the remux refuses its own, so no cue of the trimmed track goes. The original stays."""
    settings(keep_days=0)
    path = str(tmp_path / "subtitle.mkv")
    shutil.copy(mkvs / "subtitle.mkv", path)
    j, before, real = REAL_MKVMERGE(path), open(path, "rb").read(), decide.trim_srt
    monkeypatch.setattr(decide, "trim_srt", lambda text, limit: (lambda t, n: (t + "\n3\nno timing line\n", n))(*real(text, limit)))
    code, result, info = hook.repack(path, j, os.stat(path), True, hook.video_check(path)[2]["header"])
    assert code == "header_repair_failed" and result.startswith("header repair failed: mkvmerge exited 1: ") and "timestamp line" in result, result
    assert open(path, "rb").read() == before


def test_a_header_repair_keeps_the_spaces_at_the_ends_of_subtitle_lines(zp, tmp_path, settings):
    """By default mkvmerge cuts the spaces and tabs at the ends of each line of a text subtitle. A header repair keeps
    every packet byte for byte, see KEEP_PACKETS."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "ws.mkv")
    text = lambda: REAL_RUN(["mkvextract", path, "tracks", f"2:{tmp_path / 'out.srt'}"], check=True, capture_output=True) and (tmp_path / "out.srt").read_bytes()
    before = text()
    assert b"Hello   \r\n" in before or b"Hello   \n" in before, before
    code, result, info = hook.repack(path, j, os.stat(path), True, header(path, j))
    assert result == "header repaired" and text() == before, (result, info)


@pytest.fixture(scope="module")
def german(tmp_path_factory):
    """A German locale in a folder of its own, for LOCPATH. localedef builds it from the host's locale sources."""
    d = tmp_path_factory.mktemp("locale")
    r = REAL_RUN(["localedef", "-i", "de_DE", "-f", "UTF-8", str(d / "de_DE.UTF-8")], capture_output=True, text=True)
    if not (d / "de_DE.UTF-8").is_dir():
        pytest.skip(f"needs localedef and the de_DE locale source: {r.stderr[-200:]}")
    return d


def test_a_header_repair_reads_the_resync_of_mkvmerge_in_any_locale(zp, tmp_path, settings, monkeypatch, german):
    """mkvmerge prints its warnings in the language of the locale, and under a German one resynced() would read none of
    them. Each remux asks mkvmerge for English, so the repair of the enclosed zeros passes."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "ff_zp_ed.mkv")
    at = os.path.getsize(zp / "ff.mkv")
    monkeypatch.setenv("LOCPATH", str(german))
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.delenv("LANGUAGE", raising=False)
    r = REAL_RUN(["mkvmerge", "-q", "-o", str(tmp_path / "de.mkv"), path], capture_output=True, text=True)
    if not r.stdout.startswith("Warnung: "):   # the test can fail only where mkvmerge speaks German
        pytest.skip(f"mkvmerge printed no German warning here, exit {r.returncode}: {(r.stdout + r.stderr)[-300:]!r}")
    code, result, info = hook.repack(path, j, os.stat(path), True, header(path, j, enclosed=at, enclosed_size=ZEROS))
    assert result == "header repaired" and "Warning: Resync failed" in info["warnings"] and "Warnung" not in info["warnings"], (result, info)


def test_a_trim_after_a_removed_track_keeps_the_uid_of_the_trimmed_track(mkvs, tmp_path, settings):
    """The removed track before the trimmed one moves it up one place. A selector names a track by its UID, and the
    trimmed track keeps its UID. So the edits show by the selectors of the original, and the undo by them brings the
    original's default flags and tags back. unedited() takes the edits back by UID, and replanned() takes the row's
    edits as they are, see process.write()."""
    settings(keep_days=0)
    path = str(tmp_path / "m.mkv")
    REAL_RUN(["mkvmerge", "-q", "-o", path, str(mkvs / "good.mkv"), "--language", "0:ita", str(mkvs / "ita.srt"), "--language", "0:spa",
              str(mkvs / "late.srt")], check=True)
    j = REAL_MKVMERGE(path)
    hp = hook.video_check(path)[2]["header"]
    assert (hp["remove"], hp["trim"]) == ([2], [3]), hp
    s = decide.classify(j)[2]
    edits = [[s["sel"], "fr-CA", s["tag"], decide.LANG_EDIT], [s["sel"], 1 - s["default"], s["default"]]]
    code, result, info = hook.repack(path, j, os.stat(path), True, hp, edits)
    assert result == "subtitles removed" and list(info["trimmed"]) == [3], (result, info)
    new = REAL_MKVMERGE(path)
    assert decide.classify(new)[1]["sel"] == s["sel"] and decide.unapplied(new, edits) == [], decide.classify(new)
    kept = lambda x: [t for t in x["tracks"] if t["properties"].get("language") != "ita"]
    flags = lambda x: [t["default"] for t in hook.flags(dict(x, tracks=kept(x)))]   # the trim changes the role, see the stats tags
    langs = lambda x: [(t["properties"].get("language"), t["properties"].get("language_ietf")) for t in kept(x)]
    back = hook.unedited(new, j, edits)   # the new file as it reads with no edit carried
    assert flags(back) == flags(j) and langs(back) == langs(j)
    undo = str(tmp_path / "undo.mkv")
    shutil.copy(path, undo)
    REAL_RUN(["mkvpropedit", "-q", undo, *hook.propedit_args(edits, 2)], check=True)   # the undo line of write()
    assert flags(REAL_MKVMERGE(undo)) == flags(j) and langs(REAL_MKVMERGE(undo)) == langs(j)
    ctx = types.SimpleNamespace(row={"edits": edits}, j=new)
    assert hook.replanned(ctx, edits)["against"]["edits"] == sorted(edits, key=json.dumps)


def test_a_walk_past_64_bits_refuses_the_repair_with_its_reason(zp, tmp_path, settings, monkeypatch):
    """The walk of a file whose subtitle block lies 2^64 ns less 16 ms in, as a HandBrake file of libmkv 0.6.5 stores
    one, raises ValueError, see matroska.track_frames(). lace_guess() runs outside the try of repack() and resub(),
    and it finds no lace. The dry run plans, and each remux refuses with the walk's reason. The original stays."""
    settings(keep_days=0)
    path, j = copy(zp, tmp_path, "src.mkv")
    hp, before, real = header(path, j), open(path, "rb").read(), hook.matroska.cluster

    def far(b, start, end, open_, blocks, reads):   # the first block of the original's first subtitle, at the time of libmkv's Cluster
        got = real(b, start, end, open_, blocks, reads)
        if blocks.get(3) and len(b) == len(before):
            blocks[3][0] = (((1 << 64) - 16551616) // 1000000, *blocks[3][0][1:])
        return got
    monkeypatch.setattr(hook.matroska, "cluster", far)
    hook.matroska.WALKS.clear()
    with pytest.raises(ValueError, match="track 3 stores a time or a duration of 2"):
        hook.matroska.frames(path)
    assert hook.lace_guess(path, j) == ([], None) and hook.repack(path, j, os.stat(path), False, hp)[0] == "would_repair_header"
    code, result, _ = hook.repack(path, j, os.stat(path), True, hp)
    assert code == "header_repair_failed" and "the video frames of the original are not all known" in result and "2^63 ns" in result, result
    code, result, _ = hook.resub(path, j, os.stat(path), True, {2: {"rate": "1/1", "offset": 0.5}}, hp=hp)
    assert code == "subtitle_remux_failed" and "2^63 ns" in result and open(path, "rb").read() == before, result
