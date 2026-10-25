# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The rewrites of mkvmerge 102 that a remux may make, one row each of remux.REWRITES (docs/design.md, "mkvmerge
rewrites"). For each row, the change it accepts passes, and each near change fails. Every header and probe is made up."""
import copy

import pytest

from test_arr_media_guard import hook, sub_probe


def lace(*packets):
    """A Xiph lacing of packets (RFC 9559 10.3.2), as a Vorbis CodecPrivate holds its 3 headers."""
    sizes = b"".join(b"\xff" * (len(p) // 255) + bytes([len(p) % 255]) for p in packets[:-1])
    return bytes([len(packets) - 1]) + sizes + b"".join(packets)


def comments(vendor, *items, framing=b"\x01"):
    """A Vorbis comment header (Vorbis I 5.2.3): the vendor, the count, each comment with its length, the framing byte."""
    out = b"\x03vorbis" + len(vendor).to_bytes(4, "little") + vendor + len(items).to_bytes(4, "little")
    return out + b"".join(len(c).to_bytes(4, "little") + c for c in items) + framing


IDENT = b"\x01vorbis" + bytes(4) + b"\x02" + (44100).to_bytes(4, "little") + bytes(12) + b"\xb8\x01"   # 30 bytes
SETUP = b"\x05vorbis" + bytes(range(256)) * 3   # past 255 bytes, as every setup header
VENDOR = b"Xiph.Org libVorbis I 20200704 (Reducing Environment)"
OLD_VORBIS = lace(IDENT, comments(VENDOR, b"encoder=Lavc61.19.101", b"TITLE=A made-up film"), SETUP)
EMPTIED = lace(IDENT, comments(VENDOR), SETUP)


def kept(old, new, codec, ended=False):
    """header_kept() of the bytes old and new, or of hex strings as they are."""
    hexed = lambda x: x.hex() if isinstance(x, bytes) else x
    return hook.header_kept(hexed(old), hexed(new), codec, ended)


def test_the_vorbis_row_takes_the_emptied_comment_list():
    """mkvmerge empties the comment list of a Vorbis header and keeps its vendor. That passes, also when the comment
    header holds 255 bytes or more, so its lace size takes two bytes."""
    long = lace(IDENT, comments(VENDOR, b"DESCRIPTION=" + b"x" * 300), SETUP)
    assert kept(OLD_VORBIS, EMPTIED, "A_VORBIS") and kept(long, EMPTIED, "A_VORBIS")
    assert hook.xiph(long)[1] == comments(VENDOR, b"DESCRIPTION=" + b"x" * 300) and long[2] == 255
    assert hook.xiph(EMPTIED[:40]) is None and hook.vorbis_comments(comments(VENDOR)[:60]) is None, "cut inside a packet"
    assert not kept(OLD_VORBIS, EMPTIED, "A_OPUS"), "another codec takes no Vorbis row"


@pytest.mark.parametrize("why, new", [
    ("a changed setup header", lace(IDENT, comments(VENDOR), SETUP[:-1] + b"\0")),
    ("a changed identification header", lace(IDENT[:-1] + b"\0", comments(VENDOR), SETUP)),
    ("a changed vendor", lace(IDENT, comments(b"Lavf61.7.100"), SETUP)),
    ("a vendor of the same length", lace(IDENT, comments(VENDOR.replace(b"Xiph", b"Xaph")), SETUP)),
    ("an added comment", lace(IDENT, comments(VENDOR, b"encoder=Lavc61.19.101"), SETUP)),
    ("a framing byte of 0", lace(IDENT, comments(VENDOR, framing=b"\0"), SETUP)),
    ("a byte after the framing byte", lace(IDENT, comments(VENDOR) + b"\0", SETUP)),
    ("two packets", lace(IDENT, comments(VENDOR) + SETUP)),
    ("truncated data", EMPTIED[:2]),
    ("bad hex", EMPTIED.hex() + "z"),
])
def test_the_vorbis_row_refuses_each_near_change(why, new):
    assert not kept(OLD_VORBIS, new, "A_VORBIS"), why


@pytest.mark.parametrize("why, old", [
    ("a picture comment", lace(IDENT, comments(VENDOR, b"METADATA_BLOCK_PICTURE=AAAAAwAAAAppbWFnZS9qcGVn"), SETUP)),
    ("a picture comment in lower case", lace(IDENT, comments(VENDOR, b"metadata_block_picture=AAAA"), SETUP)),
    ("a comment length past the end", lace(IDENT, comments(VENDOR, b"TITLE=x")[:-4], SETUP)),
    ("a lace size of 255 or more past the end", b"\x02\xff\xff\xff"),
    ("no comment header", lace(IDENT, b"\x04vorbis" + comments(VENDOR)[7:], SETUP)),
])
def test_the_vorbis_row_refuses_an_old_header_it_cannot_read(why, old):
    """mkvmerge makes a picture comment an attachment, so that refusal stays (C8). A header whose lengths do not read
    gives no row a vendor to keep."""
    assert not kept(old, EMPTIED, "A_VORBIS"), why


@pytest.mark.parametrize("why, old, new", [
    ("two packets in both", lace(IDENT, comments(VENDOR, b"TITLE=x")), lace(IDENT, comments(VENDOR))),
    ("four packets in both", lace(IDENT, comments(VENDOR, b"TITLE=x"), SETUP, b"x"), lace(IDENT, comments(VENDOR), SETUP, b"x")),
])
def test_the_vorbis_row_takes_three_packets_only(why, old, new):
    assert not kept(old, new, "A_VORBIS"), why


def test_a_picture_comment_refuses_in_props_fault_too():
    """The subtitle remux counts the attachments, and a picture comment that became one refuses there as well."""
    j = sub_probe(("eng", True, {}))
    j["tracks"][1]["properties"].update(codec_id="A_VORBIS", codec_private_data=OLD_VORBIS.hex())
    new = copy.deepcopy(j)
    new["tracks"][1]["properties"]["codec_private_data"] = EMPTIED.hex()
    assert hook.props_fault(j, new) is None
    assert hook.props_fault(j, dict(new, attachments=[{"file_name": "cover.jpg"}])) == "the attachments changed from 0 to 1"


VP9 = bytes([1, 1, 0, 2, 1, 31, 3, 1, 8, 4, 1, 1])   # profile 0, level 3.1, bit depth 8, chroma subsampling 1


def test_the_vp9_row_takes_feature_records_where_the_track_had_no_header():
    assert kept(None, VP9, "V_VP9") and kept(None, VP9[:3] + VP9[6:9], "V_VP9")
    assert not kept(None, VP9, "V_VP8"), "another codec takes no VP9 row"


@pytest.mark.parametrize("why, old, new", [
    ("a record with ID 5", None, VP9[:9] + bytes([5, 1, 0])),
    ("a record with ID 0", None, bytes([0, 1, 0])),
    ("a record of length 2", None, VP9[:9] + bytes([4, 2, 1, 0])[:3]),
    ("a repeated ID", None, VP9 + VP9[:3]),
    ("records out of order", None, VP9[6:9] + VP9[:6] + VP9[9:]),
    ("a lone ID 1", None, VP9[:3]),
    ("no ID 3", None, VP9[:6] + VP9[9:]),
    ("no ID 1", None, VP9[3:]),
    ("ID 1 twice in a row", None, VP9[:3] + VP9),
    ("a cut record", None, VP9[:-1]),
    ("a header the original had", VP9[:3], VP9),
    ("an empty header", None, b""),
    ("bad hex", None, VP9.hex() + "z"),
])
def test_the_vp9_row_refuses_each_near_change(why, old, new):
    assert not kept(old, new, "V_VP9"), why


def test_a_new_uid_of_a_file_makemkv_wrote_refuses():
    """mkvmerge gives every track of a file MakeMKV wrote a new random UID. No row accepts that (docs/design.md,
    "mkvmerge rewrites"), so the remux refuses, as for any other app."""
    j = sub_probe(("eng", False, {}))
    j["container"]["properties"]["writing_application"] = "MakeMKV v1.17.5 linux(x64-release)"
    new = copy.deepcopy(j)
    for t in new["tracks"]:
        t["properties"]["uid"] += 100
    assert hook.props_fault(j, new) == "track 0 changed its uid"


def test_the_bcp47_rows_take_only_a_tag_mkvmerge_adds():
    """Rows 1 and 2 take a tag that mkvmerge adds. A track that had a BCP 47 tag keeps it, so en-US to en refuses. A
    new legacy tag passes only beside a BCP 47 tag of the old track."""
    assert hook.props_fault(*legacy_pair("eng", "eng", "en", had="en-US")) == "track 1 changed its language_ietf"
    assert not hook.legacy_added({"language": "eng"}, {"language": "ita", "language_ietf": "it"}, {})


def legacy_pair(old, legacy, ietf, had=None):
    """(the probe of a made-up film whose audio has the legacy language old and the BCP 47 tag had, its remux with
    legacy and ietf)."""
    j = sub_probe(audio=(("eng", True),))
    p = j["tracks"][1]["properties"]
    p.pop("language_ietf", None)
    p.update(language=old, **({"language_ietf": had} if had else {}))
    new = copy.deepcopy(j)
    new["tracks"][1]["properties"].update(language=legacy, language_ietf=ietf)
    return j, new


def test_the_legacy_row_takes_a_639_3_code_kept_as_bcp_47():
    """mkvmerge writes a 639-3 code that its 639-2 table lacks, as yue, as the BCP 47 tag, and the closest 639-2 code
    as the legacy tag (C6 option B)."""
    assert hook.props_fault(*legacy_pair("yue", "chi", "yue")) is None
    assert hook.props_fault(*legacy_pair("yue", "und", "yue")) is None
    assert hook.props_fault(*legacy_pair("cmn", "chi", "cmn")) is None


@pytest.mark.parametrize("why, pair", [
    ("any, the Anyin language in BCP 47", ("any", "und", "any")),
    ("a chi file with no legacy change", ("chi", "chi", "chi")),
    ("a chi file whose legacy tag changed", ("chi", "und", "chi")),
    ("another BCP 47 tag", ("yue", "chi", "zh")),
    ("a legacy tag the table lacks", ("yue", "cmn", "yue")),
    ("a legacy tag of another language", ("yue", "eng", "yue")),
    ("zsm to may, which lang_key() keeps apart", ("zsm", "may", "zsm")),
    ("an old BCP 47 tag", ("yue", "chi", "yue", "zh-yue")),
])
def test_the_legacy_row_refuses_each_near_change(why, pair):
    assert hook.props_fault(*legacy_pair(*pair)) is not None, why


def test_the_legacy_row_refuses_with_no_language_table(monkeypatch):
    monkeypatch.setattr(hook, "langs", lambda: ({}, {}))
    assert hook.props_fault(*legacy_pair("yue", "chi", "yue")) is not None


HEAD = [b"[Script Info]", b"ScriptType: v4.00+", b"", b"[V4+ Styles]", b"Format: Name, Fontname, Fontsize", b"Style: Default,Arial,28"]
FORMAT = b"Format: Layer, Start, End, Style, Actor, MarginL, MarginR, MarginV, Effect, Text"
CRLF = lambda lines: b"".join(x + b"\r\n" for x in lines)   # noqa: E731
DONE = CRLF(HEAD + [b"", b"[Events]", FORMAT]) + b"\r\n"   # what the round trip of ended_track() writes for HEAD


@pytest.mark.parametrize("why, old, new", [
    ("a HandBrake header: a NUL end, no [Events]", CRLF(HEAD) + b"\0", DONE),
    ("no NUL", CRLF(HEAD), DONE),
    ("LF line ends", b"\n".join(HEAD) + b"\n", DONE),
    ("3 blank lines at the end", CRLF(HEAD) + b"\r\n\r\n\r\n", DONE),
    ("an [Events] section kept", CRLF(HEAD + [b"", b"[Events]", FORMAT]) + b"\r\n\r\n", DONE),
    ("an [Events] section with one line end", CRLF(HEAD + [b"", b"[Events]", FORMAT]), DONE),
    ("the same header", DONE, DONE),
])
def test_the_ass_row_takes_the_header_the_round_trip_writes(why, old, new):
    assert kept(old, new, "S_TEXT/ASS", True), why


@pytest.mark.parametrize("why, old, new, ended", [
    ("a changed line", CRLF(HEAD) + b"\0", DONE.replace(b"Arial", b"Ariel"), True),
    ("a reordered line", CRLF(HEAD) + b"\0", CRLF([HEAD[1], HEAD[0], *HEAD[2:], b"", b"[Events]", FORMAT]) + b"\r\n", True),
    ("a dropped line", CRLF(HEAD) + b"\0", CRLF(HEAD[:-1] + [b"", b"[Events]", FORMAT]) + b"\r\n", True),
    ("a dropped blank line inside", CRLF(HEAD) + b"\0", CRLF(HEAD[:2] + HEAD[3:] + [b"", b"[Events]", FORMAT]) + b"\r\n", True),
    ("two blank lines at the end", CRLF(HEAD) + b"\0", DONE + b"\r\n", True),
    ("no blank line at the end", CRLF(HEAD) + b"\0", DONE[:-2], True),
    ("the NUL kept", CRLF(HEAD) + b"\0", DONE + b"\0", True),
    ("a second [Events] section", CRLF(HEAD + [b"", b"[Events]", FORMAT]), CRLF(HEAD + [b"", b"[Events]", FORMAT, b"", b"[Events]", FORMAT]) + b"\r\n", True),
    ("[events] in lower case", CRLF(HEAD + [b"[events]", FORMAT]), CRLF(HEAD + [b"[events]", FORMAT, b"", b"[Events]", FORMAT]) + b"\r\n", True),
    ("the SSA Format line", CRLF(HEAD) + b"\0", DONE.replace(b"Layer", b"Marked").replace(b"Actor", b"Name"), True),
    ("a track that ended_track() did not write", CRLF(HEAD) + b"\0", DONE, False),
    ("bad hex", "zz", DONE, True),
])
def test_the_ass_row_refuses_each_near_change(why, old, new, ended):
    assert not kept(old, new, "S_TEXT/ASS", ended), why


def test_every_row_names_its_scope_change_and_one_function():
    """A new rewrite is one more row: the codecs, the properties, the change and the function that accepts it."""
    assert all(callable(r.accepts) and r.change and r.props and (r.codecs is None or r.codecs) for r in hook.REWRITES)
    assert len({r.accepts for r in hook.REWRITES}) == len(hook.REWRITES)
    assert hook.rewritten("codec_private_data", {"codec_id": "A_VORBIS", "codec_private_data": OLD_VORBIS.hex()},
                          {"codec_id": "A_VORBIS", "codec_private_data": EMPTIED.hex()}, {}).accepts is hook.vorbis_header
