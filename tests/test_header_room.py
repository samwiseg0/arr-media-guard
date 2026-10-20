# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The header check of bytes that a flag edit would put inside the Matroska Segment (docs/design.md, "Header repair").
mkvpropedit grows Tracks into the first Void that holds it. When no Void does, it writes Tracks at the end of the file
and extends the Segment over any bytes past its end. Those bytes then break the EBML layout, and mkvmerge warns on
them. The files are made up, see the layouts fixture."""
import os
import subprocess

from test_arr_media_guard import cue_index, el, hook, layouts, made_mkv, set_duration  # noqa: F401

ENCLOSED = "the Matroska Segment holds 700 zero bytes after its Cues, and no element starts there"
JOINED = "the bytes past the Segment end start another Matroska file"


def with_tail(src, path, tail):
    path.write_bytes(src.read_bytes() + tail)
    return str(path)


def cues_bytes(path):
    """The bytes of the Cues data, which header_probe() reads whole."""
    D, b = hook.arr_decide, open(path, "rb").read()
    ds = D.segment_start(b)[0]
    e = D.element(b, ds + D.front_seeks(b, ds)[D.CUES][0])
    return e[2]


def test_bytes_a_flag_edit_would_enclose_are_read_only_for_an_edit(layouts, tmp_path):
    """Owner 2026-10-19: bytes past the Segment end are no header issue, whatever their content, and only a run with a
    flag edit remuxes them. Without an edit the check reads the start and the Cues, as on a clean file. With one it
    reads the stream ends for the remux that carries the edit. A tail over TAIL_MIN counts too, and stays an issue."""
    for name, src, tail in (("ff_zp", "ffmpeg", bytes(700)), ("ff_nz", "ffmpeg", bytes(699) + b"\x07"), ("mm_zp", "src", bytes(700))):
        path = with_tail(layouts[src], tmp_path / f"{name}.mkv", tail)
        hp = hook.header_of(path)
        assert (hp["edit_tail"], hp["edit_blocked"], hp["issue"], hp["tail"], hp["first"], hp["end"], hp["read"]) == \
            (700, None, [], 0, False, None, hook.HEADER_READ + cues_bytes(path)), (name, hp)
        hp = hook.header_of(path, edit=True)
        assert (hp["edit_tail"], hp["edit_blocked"], hp["issue"], hp["blocked"], hp["end"]) == (700, [], [], [], 30.023), (name, hp)
    big = hook.TAIL_MIN + 1
    hp = hook.header_of(with_tail(layouts["ffmpeg"], tmp_path / "big.mkv", bytes(big)))
    assert (hp["edit_tail"], hp["tail"], hp["issue"], hp["edit_blocked"]) == (big, big, [f"the file holds {big} bytes past the end of its Matroska Segment"], []), hp


def test_the_tail_guards_block_the_remux_that_carries_the_edit(layouts, tmp_path):
    """Bytes that start another Matroska file, and a Segment whose streams stop before its stated length, block the
    remux for the edit, as they block the remux of a tail over TAIL_MIN. They stay out of blocked and not_whole, so the
    check says what it said before. A subtitle event far past the streams sets the stated length, and the trim then
    ends it, so it is no sign of a cut Segment."""
    path = with_tail(layouts["ffmpeg"], tmp_path / "joined.mkv", layouts["src"].read_bytes()[:700])   # a second file, cut short
    assert hook.header_of(path)["edit_blocked"] is None
    hp = hook.header_of(path, edit=True)
    assert (hp["edit_tail"], hp["issue"], hp["blocked"], hp["edit_blocked"]) == (700, [], [], [JOINED]), hp
    path = with_tail(layouts["ffmpeg"], tmp_path / "cut.mkv", bytes(700))
    set_duration(path, 3600)
    hp = hook.header_of(path)   # the wrong duration reads the stream ends without an edit
    assert (hp["blocked"], hp.get("not_whole"), len(hp["edit_blocked"])) == ([], None, 1), hp
    assert hp["edit_blocked"][0].startswith("the file has 700 bytes of extra data at its end, and the video and audio stop before"), hp
    d = tmp_path / "late"; d.mkdir()   # a SubRip event at 10:00 in a 30-second Lavf file
    (d / "s.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:10:00,000 --> 00:10:02,000\nLate\n")
    subprocess.run(["mkvmerge", "-q", "-o", str(d / "src.mkv"), str(layouts["av"]), str(d / "s.srt")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(d / "src.mkv"), "-map", "0", "-c", "copy", str(d / "lavf.mkv")], check=True)
    hp = hook.header_of(with_tail(d / "lavf.mkv", d / "late.mkv", bytes(700)), edit=True)
    assert (hp["issue"], hp["blocked"], hp.get("not_whole"), hp["edit_blocked"], hp["trim"]) == \
        (["a subtitle event runs to 10:02, past the video and the audio at 0:30"], [], None, [], [2]), hp


def test_zeros_a_flag_edit_enclosed_in_the_segment_are_a_header_issue(layouts, tmp_path):
    """mkvpropedit extends the Segment over the zeros past its end and writes the grown Tracks after them. enclosed is
    where the zeros start, the old Segment end, and the stream ends are read up to there. The repair runs first,
    because ffmpeg cannot read past the zeros for a subtitle remux. Bytes past the new Segment
    end change nothing. One byte that is not zero in the run leaves no enclosed zeros, as zero_pad() does. Chapters
    that mkvpropedit adds after the zeros are found too."""
    path = with_tail(layouts["ffmpeg"], tmp_path / "edited.mkv", bytes(700))
    subprocess.run(["mkvpropedit", "-q", path, "--edit", "track:a1", "--set", "flag-original=1", "--set", "language=fre"], check=True)
    hp = hook.header_of(path)
    at = os.path.getsize(layouts["ffmpeg"])
    assert (hp["enclosed"], hp["enclosed_size"], hp["issue"], hp["blocked"], hp["first"], hp["edit_tail"], hp["end"]) == \
        (at, 700, [ENCLOSED], [], True, 0, 30.023), hp
    with open(path, "rb") as f:   # the start, the Cues, the walk past them, and the file up to the zeros for the stream ends
        assert hp["read"] == hook.HEADER_READ + cues_bytes(path) + hook.zero_gap(f, at, os.path.getsize(path))[1] + at
    hp = hook.header_of(with_tail(tmp_path / "edited.mkv", tmp_path / "edited_tail.mkv", bytes(100)))
    assert (hp["enclosed"], hp["issue"], hp["edit_tail"]) == (at, [ENCLOSED], 100), hp
    with open(path, "r+b") as f:
        f.seek(at + 350); f.write(b"\x07")
    hp = hook.header_of(path)
    assert (hp["enclosed"], hp["enclosed_size"], hp["issue"], hp["first"]) == (None, 0, [], False), hp
    path = with_tail(layouts["ffmpeg"], tmp_path / "chapters.mkv", bytes(700))
    subprocess.run(["mkvpropedit", "-q", path, "--chapters", str(layouts["src"].parent / "ch.txt")], check=True)
    assert hook.header_of(path)["enclosed"] == at
    D = hook.arr_decide   # the walk counts each byte it reads: a header, the zeros up to the Segment end, a header
    info, tracks = el(D.INFO, el(D.TIMESTAMP_SCALE, (10 ** 6).to_bytes(4, "big"))), el(0x1654AE6B, bytes(40))
    at = made_mkv(tmp_path / "pad.mkv", [info, el(D.CLUSTER, el(D.TIMESTAMP, b"\x00")), cue_index([(0, 0)]), bytes(700), tracks], 2)
    with open(tmp_path / "pad.mkv", "rb") as f:
        assert hook.zero_gap(f, at[3], os.path.getsize(tmp_path / "pad.mkv")) == ((at[3], 700), 16 + 700 + len(tracks) + 16)


def test_the_hooks_own_failed_edit_repairs_first(layouts, tmp_path, settings):
    """A Segment size past the end of the file after the hook's own failed edit is a header issue, and its repair runs
    before the checks read the file, as before. SHORT_CERTAIN bytes or more past the end is no header issue."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    for more, issue in ((100, ["the Segment size promises 100 bytes past the end of the file after the hook's own edit of it failed"]),
                        (hook.arr_decide.SHORT_CERTAIN, [])):
        b = bytearray(layouts["src"].read_bytes())
        ds, seg = hook.arr_decide.segment_start(bytes(b[:64]))
        b[ds - 8:ds] = (1 << 56 | (seg + more)).to_bytes(8, "big")   # mkvmerge writes an 8-byte Segment size
        path = tmp_path / f"failed{more}.mkv"; path.write_bytes(b)
        hook.log(dict(path=str(path), result="editing"))   # an editing line with no edited line after it
        hp = hook.header_of(str(path))
        assert (hp["failed_edit"], hp["issue"], hp["first"]) == (True, issue, bool(issue)), hp


def test_a_clean_file_reads_the_same_bytes_as_before(layouts, tmp_path):
    """A clean file reads its first HEADER_READ bytes and its Cues, as before the checks of this file. mkvmerge lists
    Tags right at the end of the Cues and Lavf lists nothing after them, so the walk for enclosed zeros reads nothing."""
    for name in ("src", "ffmpeg"):
        hp = hook.header_of(str(layouts[name]))
        assert (hp["issue"], hp["read"]) == ([], hook.HEADER_READ + cues_bytes(layouts[name])), name
