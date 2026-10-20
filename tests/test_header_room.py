# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The header check of bytes past the Matroska Segment end and of bytes that a flag edit put inside the Segment
(docs/design.md, "Header repair"). mkvpropedit grows Tracks into the first Void that holds it. When no Void does, it
writes Tracks at the end of the file and extends the Segment over any bytes past its end. Those bytes then break the
EBML layout, and mkvmerge warns on them. The files are made up, see the layouts fixture."""
import os
import subprocess
import sys

from test_arr_media_guard import cue_index, el, hook, layouts, made_mkv, set_duration  # noqa: F401

ENCLOSED = "the Matroska Segment holds 700 zero bytes after its Cues, and no element starts there"
JOINED = "the bytes past the Segment end start another Matroska file"
PAD = "the file holds 700 bytes past the end of its Matroska Segment"


def with_tail(src, path, tail):
    path.write_bytes(src.read_bytes() + tail)
    return str(path)


def cues_bytes(path):
    """The bytes of the Cues data, which header_probe() reads whole."""
    D, b = hook.arr_decide, open(path, "rb").read()
    ds = D.segment_start(b)[0]
    e = D.element(b, ds + D.front_seeks(b, ds)[D.CUES][0])
    return e[2]


def outside(path):
    """(the bytes of the level-1 elements that are no Cluster and end past the first HEADER_READ bytes, with 24 more
    for each one's header and the read of it, the bytes of the Clusters). The RFC 9559 check may read the first, never the second."""
    D, b = hook.arr_decide, open(path, "rb").read()
    ds, seg = D.segment_start(b)
    top = list(D.children(b, ds, ds + seg))
    return (sum(24 + n for i, d, n in top if i != D.CLUSTER and d + n > hook.HEADER_READ),
            sum(n for i, d, n in top if i == D.CLUSTER))


def test_bytes_past_the_segment_end_are_a_header_issue(layouts, tmp_path):
    """Owner 2026-10-20: bytes past the Segment end are a header issue at any count, whatever their content, and the
    run repairs them when it finds them. The check reads the stream ends for that repair. Up to TAIL_MIN they are
    padding, which the library pass leaves alone. More is a tail, as before. Docket 54 (2026-10-19) kept them until a
    flag edit."""
    for name, src, tail in (("ff_zp", "ffmpeg", bytes(700)), ("ff_nz", "ffmpeg", bytes(699) + b"\x07"), ("mm_zp", "src", bytes(700))):
        path = with_tail(layouts[src], tmp_path / f"{name}.mkv", tail)
        hp = hook.header_of(path)
        assert (hp["edit_tail"], hp["edit_blocked"], hp["issue"], hp["tail"], hp["first"], hp["blocked"], hp["end"], hp["rfc"]) == \
            (700, [], [PAD], 0, False, [], 30.023, {"padding": ["700 bytes past the Segment end"]}), (name, hp)
        assert hook.padding_only(hp)
    big = hook.TAIL_MIN + 1
    hp = hook.header_of(with_tail(layouts["ffmpeg"], tmp_path / "big.mkv", bytes(big)))
    assert (hp["edit_tail"], hp["tail"], hp["issue"], hp["edit_blocked"], hp["rfc"]) == \
        (big, big, [f"the file holds {big} bytes past the end of its Matroska Segment"], [], {"tail": [f"{big} bytes past the Segment end"]}), hp
    assert not hook.padding_only(hp)


def test_the_tail_guards_block_the_remux_that_carries_the_edit(layouts, tmp_path):
    """Bytes that start another Matroska file, and a Segment whose streams stop before its stated length, block the
    remux of padding, as they block the remux of a tail over TAIL_MIN. They stay out of blocked and not_whole, so the
    check says what it said before. A subtitle event far past the streams sets the stated length, and the trim then
    ends it, so it is no sign of a cut Segment."""
    path = with_tail(layouts["ffmpeg"], tmp_path / "joined.mkv", layouts["src"].read_bytes()[:700])   # a second file, cut short
    hp = hook.header_of(path)
    assert (hp["edit_tail"], hp["issue"], hp["blocked"], hp["edit_blocked"]) == (700, [PAD], [], [JOINED]), hp
    path = with_tail(layouts["ffmpeg"], tmp_path / "cut.mkv", bytes(700))
    set_duration(path, 3600)
    hp = hook.header_of(path)
    assert (hp["blocked"], hp.get("not_whole"), len(hp["edit_blocked"])) == ([], None, 1), hp
    assert hp["edit_blocked"][0].startswith("the file has 700 bytes of extra data at its end, and the video and audio stop before"), hp
    d = tmp_path / "late"; d.mkdir()   # a SubRip event at 10:00 in a 30-second Lavf file
    (d / "s.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:10:00,000 --> 00:10:02,000\nLate\n")
    subprocess.run(["mkvmerge", "-q", "-o", str(d / "src.mkv"), str(layouts["av"]), str(d / "s.srt")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(d / "src.mkv"), "-map", "0", "-c", "copy", str(d / "lavf.mkv")], check=True)
    hp = hook.header_of(with_tail(d / "lavf.mkv", d / "late.mkv", bytes(700)))
    assert (hp["issue"], hp["blocked"], hp.get("not_whole"), hp["edit_blocked"], hp["trim"]) == \
        ([PAD, "a subtitle event runs to 10:02, past the video and the audio at 0:30"], [], None, [], [2]), hp


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
    with open(path, "rb") as f:   # the start, the Cues, the walk past them, the file up to the zeros for the stream ends, and the
        least = hook.HEADER_READ + cues_bytes(path) + hook.zero_gap(f, at, os.path.getsize(path))[1] + at   # RFC 9559 check
        after = os.path.getsize(path) - at - 700 + 24   # the Tracks mkvpropedit wrote after the zeros, which outside() cannot walk to
        assert least <= hp["read"] <= least + outside(path)[0] + after, (least, hp["read"])
    hp = hook.header_of(with_tail(tmp_path / "edited.mkv", tmp_path / "edited_tail.mkv", bytes(100)))
    assert (hp["enclosed"], hp["issue"], hp["edit_tail"]) == (at, ["the file holds 100 bytes past the end of its Matroska Segment", ENCLOSED], 100), hp
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


class Recorded:
    """An open file that notes the byte range of each read in seen."""

    def __init__(self, path, mode, seen):
        self.f, self.seen = open(path, mode), seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def seek(self, *a):
        return self.f.seek(*a)

    def read(self, n=-1):
        at = self.f.tell(); b = self.f.read(n); self.seen.append((at, at + len(b)))
        return b


def test_a_clean_file_reads_no_cluster(layouts, tmp_path, monkeypatch):
    """A clean file reads its first HEADER_READ bytes, its Cues, and the level-1 elements of the RFC 9559 check, never a
    Cluster. mkvmerge lists Tags right at the end of the Cues and Lavf lists nothing after them, so the walk for
    enclosed zeros reads nothing. The decision line holds read and took. Past the first HEADER_READ bytes, the only read
    of a Cluster is the element header of the first one. That header ends the walk of the front. In att, a 100 KB
    attachment puts the first Cluster past those bytes, so a read of it shows too. clusters lists every Cluster in a
    second SeekHead, which the check never follows."""
    (tmp_path / "blob.bin").write_bytes(os.urandom(100_000))
    att = tmp_path / "att.mkv"
    subprocess.run(["mkvmerge", "-q", "-o", str(att), "--attachment-mime-type", "application/octet-stream", "--attach-file", str(tmp_path / "blob.bin"),
                    str(layouts["av"])], check=True)
    seen, D = [], hook.arr_decide
    monkeypatch.setattr(sys.modules[f"{hook._name}.checks"], "open", lambda path, mode="r": Recorded(path, mode, seen), raising=False)
    for name, path in (("src", layouts["src"]), ("ffmpeg", layouts["ffmpeg"]), ("clusters", layouts["clusters"]), ("att", att)):
        seen.clear()
        hp, (more, clusters) = hook.header_of(str(path)), outside(path)
        least = hook.HEADER_READ + cues_bytes(path)
        assert (hp["issue"], hp["rfc"]) == ([], {}) and least <= hp["read"] <= least + more and "took" in hp, (name, hp, more)
        assert more < clusters / 4, (name, more, clusters)
        b = open(path, "rb").read()
        ds, seg = D.segment_start(b)
        spans, p = [], ds   # each Cluster from the first byte of its id
        while p < ds + seg:
            e = D.element(b, p)
            spans += [(p, e[1] + e[2])] if e[0] == D.CLUSTER else []
            p = e[1] + e[2]
        assert name != "att" or spans[0][0] > hook.HEADER_READ, spans[0]
        late = [(x, y) for x, y in seen[1:] if (x, y) != (spans[0][0], spans[0][0] + 12) and any(min(y, e) > max(x, s) for s, e in spans)]
        assert seen[0] == (0, hook.HEADER_READ) and late == [], (name, late)
