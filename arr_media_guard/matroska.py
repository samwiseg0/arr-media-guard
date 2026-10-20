# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The RFC 9559 check of the Matroska elements outside the Clusters (docs/design.md, "RFC 9559 check"). check() reads
the EBML header, each level-1 element before the first Cluster, and each one a SeekHead lists. It never reads a
Cluster. It holds each element to the schema of RFC 9559 section 5.1 and to the EBML rules of RFC 8794."""
import collections, struct, zlib

from . import decide

# The elements of RFC 9559 section 5.1 outside the Clusters, one a line: the id, the path below the Segment, the type
# (m master, u uinteger, i integer, f float, s string, 8 utf-8, b binary, d date), the least and the most occurrences in
# the parent (* for no limit), the minver, then r=LOW..HIGH and l=LENGTH where the RFC sets a range or a length. A "("
# before LOW leaves LOW out of the range. The least is 1 only for a mandatory element with no default, because a reader
# takes the default of a missing one. A "+" marks a master that may also sit in itself, as ChapterAtom.
TABLE = """
0x114d9b74 SeekHead m 0-2 1
0x4dbb SeekHead/Seek m 1-* 1
0x53ab SeekHead/Seek/SeekID b 1-1 1 l=4
0x53ac SeekHead/Seek/SeekPosition u 1-1 1
0x1549a966 Info m 1-1 1
0x73a4 Info/SegmentUUID b 0-1 1 l=16
0x7384 Info/SegmentFilename 8 0-1 1
0x3cb923 Info/PrevUUID b 0-1 1 l=16
0x3c83ab Info/PrevFilename 8 0-1 1
0x3eb923 Info/NextUUID b 0-1 1 l=16
0x3e83bb Info/NextFilename 8 0-1 1
0x4444 Info/SegmentFamily b 0-* 1 l=16
0x6924 Info/ChapterTranslate m 0-* 1
0x69a5 Info/ChapterTranslate/ChapterTranslateID b 1-1 1
0x69bf Info/ChapterTranslate/ChapterTranslateCodec u 1-1 1
0x69fc Info/ChapterTranslate/ChapterTranslateEditionUID u 0-* 1
0x2ad7b1 Info/TimestampScale u 0-1 1 r=1..
0x4489 Info/Duration f 0-1 1 r=(0..
0x4461 Info/DateUTC d 0-1 1
0x7ba9 Info/Title 8 0-1 1
0x4d80 Info/MuxingApp 8 1-1 1
0x5741 Info/WritingApp 8 1-1 1
0x1654ae6b Tracks m 0-1 1
0xae Tracks/TrackEntry m 1-* 1
0xd7 Tracks/TrackEntry/TrackNumber u 1-1 1 r=1..
0x73c5 Tracks/TrackEntry/TrackUID u 1-1 1 r=1..
0x83 Tracks/TrackEntry/TrackType u 1-1 1 r=1..
0xb9 Tracks/TrackEntry/FlagEnabled u 0-1 2 r=0..1
0x88 Tracks/TrackEntry/FlagDefault u 0-1 1 r=0..1
0x55aa Tracks/TrackEntry/FlagForced u 0-1 1 r=0..1
0x55ab Tracks/TrackEntry/FlagHearingImpaired u 0-1 4 r=0..1
0x55ac Tracks/TrackEntry/FlagVisualImpaired u 0-1 4 r=0..1
0x55ad Tracks/TrackEntry/FlagTextDescriptions u 0-1 4 r=0..1
0x55ae Tracks/TrackEntry/FlagOriginal u 0-1 4 r=0..1
0x55af Tracks/TrackEntry/FlagCommentary u 0-1 4 r=0..1
0x9c Tracks/TrackEntry/FlagLacing u 0-1 1 r=0..1
0x23e383 Tracks/TrackEntry/DefaultDuration u 0-1 1 r=1..
0x234e7a Tracks/TrackEntry/DefaultDecodedFieldDuration u 0-1 4 r=1..
0x23314f Tracks/TrackEntry/TrackTimestampScale f 0-1 1 r=(0..
0x55ee Tracks/TrackEntry/MaxBlockAdditionID u 0-1 1
0x41e4 Tracks/TrackEntry/BlockAdditionMapping m 0-* 4
0x41f0 Tracks/TrackEntry/BlockAdditionMapping/BlockAddIDValue u 0-1 4 r=2..
0x41a4 Tracks/TrackEntry/BlockAdditionMapping/BlockAddIDName s 0-1 4
0x41e7 Tracks/TrackEntry/BlockAdditionMapping/BlockAddIDType u 0-1 4
0x41ed Tracks/TrackEntry/BlockAdditionMapping/BlockAddIDExtraData b 0-1 4
0x536e Tracks/TrackEntry/Name 8 0-1 1
0x22b59c Tracks/TrackEntry/Language s 0-1 1
0x22b59d Tracks/TrackEntry/LanguageBCP47 s 0-1 4
0x86 Tracks/TrackEntry/CodecID s 1-1 1
0x63a2 Tracks/TrackEntry/CodecPrivate b 0-1 1
0x258688 Tracks/TrackEntry/CodecName 8 0-1 1
0x7446 Tracks/TrackEntry/AttachmentLink u 0-1 1 r=1..
0x56aa Tracks/TrackEntry/CodecDelay u 0-1 4
0x56bb Tracks/TrackEntry/SeekPreRoll u 0-1 4
0x6624 Tracks/TrackEntry/TrackTranslate m 0-* 1
0x66a5 Tracks/TrackEntry/TrackTranslate/TrackTranslateTrackID b 1-1 1
0x66bf Tracks/TrackEntry/TrackTranslate/TrackTranslateCodec u 1-1 1
0x66fc Tracks/TrackEntry/TrackTranslate/TrackTranslateEditionUID u 0-* 1
0xe0 Tracks/TrackEntry/Video m 0-1 1
0x9a Tracks/TrackEntry/Video/FlagInterlaced u 0-1 2
0x9d Tracks/TrackEntry/Video/FieldOrder u 0-1 4
0x53b8 Tracks/TrackEntry/Video/StereoMode u 0-1 3
0x53c0 Tracks/TrackEntry/Video/AlphaMode u 0-1 3
0x53b9 Tracks/TrackEntry/Video/OldStereoMode u 0-1 1
0xb0 Tracks/TrackEntry/Video/PixelWidth u 1-1 1 r=1..
0xba Tracks/TrackEntry/Video/PixelHeight u 1-1 1 r=1..
0x54aa Tracks/TrackEntry/Video/PixelCropBottom u 0-1 1
0x54bb Tracks/TrackEntry/Video/PixelCropTop u 0-1 1
0x54cc Tracks/TrackEntry/Video/PixelCropLeft u 0-1 1
0x54dd Tracks/TrackEntry/Video/PixelCropRight u 0-1 1
0x54b0 Tracks/TrackEntry/Video/DisplayWidth u 0-1 1 r=1..
0x54ba Tracks/TrackEntry/Video/DisplayHeight u 0-1 1 r=1..
0x54b2 Tracks/TrackEntry/Video/DisplayUnit u 0-1 1
0x2eb524 Tracks/TrackEntry/Video/UncompressedFourCC b 0-1 1 l=4
0x55b0 Tracks/TrackEntry/Video/Colour m 0-1 4
0x55b1 Tracks/TrackEntry/Video/Colour/MatrixCoefficients u 0-1 4
0x55b2 Tracks/TrackEntry/Video/Colour/BitsPerChannel u 0-1 4
0x55b3 Tracks/TrackEntry/Video/Colour/ChromaSubsamplingHorz u 0-1 4
0x55b4 Tracks/TrackEntry/Video/Colour/ChromaSubsamplingVert u 0-1 4
0x55b5 Tracks/TrackEntry/Video/Colour/CbSubsamplingHorz u 0-1 4
0x55b6 Tracks/TrackEntry/Video/Colour/CbSubsamplingVert u 0-1 4
0x55b7 Tracks/TrackEntry/Video/Colour/ChromaSitingHorz u 0-1 4
0x55b8 Tracks/TrackEntry/Video/Colour/ChromaSitingVert u 0-1 4
0x55b9 Tracks/TrackEntry/Video/Colour/Range u 0-1 4
0x55ba Tracks/TrackEntry/Video/Colour/TransferCharacteristics u 0-1 4
0x55bb Tracks/TrackEntry/Video/Colour/Primaries u 0-1 4
0x55bc Tracks/TrackEntry/Video/Colour/MaxCLL u 0-1 4
0x55bd Tracks/TrackEntry/Video/Colour/MaxFALL u 0-1 4
0x55d0 Tracks/TrackEntry/Video/Colour/MasteringMetadata m 0-1 4
0x55d1 Tracks/TrackEntry/Video/Colour/MasteringMetadata/PrimaryRChromaticityX f 0-1 4 r=0..1
0x55d2 Tracks/TrackEntry/Video/Colour/MasteringMetadata/PrimaryRChromaticityY f 0-1 4 r=0..1
0x55d3 Tracks/TrackEntry/Video/Colour/MasteringMetadata/PrimaryGChromaticityX f 0-1 4 r=0..1
0x55d4 Tracks/TrackEntry/Video/Colour/MasteringMetadata/PrimaryGChromaticityY f 0-1 4 r=0..1
0x55d5 Tracks/TrackEntry/Video/Colour/MasteringMetadata/PrimaryBChromaticityX f 0-1 4 r=0..1
0x55d6 Tracks/TrackEntry/Video/Colour/MasteringMetadata/PrimaryBChromaticityY f 0-1 4 r=0..1
0x55d7 Tracks/TrackEntry/Video/Colour/MasteringMetadata/WhitePointChromaticityX f 0-1 4 r=0..1
0x55d8 Tracks/TrackEntry/Video/Colour/MasteringMetadata/WhitePointChromaticityY f 0-1 4 r=0..1
0x55d9 Tracks/TrackEntry/Video/Colour/MasteringMetadata/LuminanceMax f 0-1 4 r=0..
0x55da Tracks/TrackEntry/Video/Colour/MasteringMetadata/LuminanceMin f 0-1 4 r=0..
0x7670 Tracks/TrackEntry/Video/Projection m 0-1 4
0x7671 Tracks/TrackEntry/Video/Projection/ProjectionType u 0-1 4
0x7672 Tracks/TrackEntry/Video/Projection/ProjectionPrivate b 0-1 4
0x7673 Tracks/TrackEntry/Video/Projection/ProjectionPoseYaw f 0-1 4 r=-180..180
0x7674 Tracks/TrackEntry/Video/Projection/ProjectionPosePitch f 0-1 4 r=-90..90
0x7675 Tracks/TrackEntry/Video/Projection/ProjectionPoseRoll f 0-1 4 r=-180..180
0xe1 Tracks/TrackEntry/Audio m 0-1 1
0xb5 Tracks/TrackEntry/Audio/SamplingFrequency f 0-1 1 r=(0..
0x78b5 Tracks/TrackEntry/Audio/OutputSamplingFrequency f 0-1 1 r=(0..
0x9f Tracks/TrackEntry/Audio/Channels u 0-1 1 r=1..
0x6264 Tracks/TrackEntry/Audio/BitDepth u 0-1 1 r=1..
0xe2 Tracks/TrackEntry/TrackOperation m 0-1 3
0xe3 Tracks/TrackEntry/TrackOperation/TrackCombinePlanes m 0-1 3
0xe4 Tracks/TrackEntry/TrackOperation/TrackCombinePlanes/TrackPlane m 1-* 3
0xe5 Tracks/TrackEntry/TrackOperation/TrackCombinePlanes/TrackPlane/TrackPlaneUID u 1-1 3 r=1..
0xe6 Tracks/TrackEntry/TrackOperation/TrackCombinePlanes/TrackPlane/TrackPlaneType u 1-1 3
0xe9 Tracks/TrackEntry/TrackOperation/TrackJoinBlocks m 0-1 3
0xed Tracks/TrackEntry/TrackOperation/TrackJoinBlocks/TrackJoinUID u 1-* 3 r=1..
0x6d80 Tracks/TrackEntry/ContentEncodings m 0-1 1
0x6240 Tracks/TrackEntry/ContentEncodings/ContentEncoding m 1-* 1
0x5031 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncodingOrder u 0-1 1
0x5032 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncodingScope u 0-1 1 r=1..
0x5033 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncodingType u 0-1 1
0x5034 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentCompression m 0-1 1
0x4254 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentCompression/ContentCompAlgo u 0-1 1
0x4255 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentCompression/ContentCompSettings b 0-1 1
0x5035 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncryption m 0-1 1
0x47e1 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncryption/ContentEncAlgo u 0-1 1
0x47e2 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncryption/ContentEncKeyID b 0-1 1
0x47e7 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncryption/ContentEncAESSettings m 0-1 4
0x47e8 Tracks/TrackEntry/ContentEncodings/ContentEncoding/ContentEncryption/ContentEncAESSettings/AESSettingsCipherMode u 1-1 4 r=1..
0x1c53bb6b Cues m 0-1 1
0xbb Cues/CuePoint m 1-* 1
0xb3 Cues/CuePoint/CueTime u 1-1 1
0xb7 Cues/CuePoint/CueTrackPositions m 1-* 1
0xf7 Cues/CuePoint/CueTrackPositions/CueTrack u 1-1 1 r=1..
0xf1 Cues/CuePoint/CueTrackPositions/CueClusterPosition u 1-1 1
0xf0 Cues/CuePoint/CueTrackPositions/CueRelativePosition u 0-1 4
0xb2 Cues/CuePoint/CueTrackPositions/CueDuration u 0-1 4
0x5378 Cues/CuePoint/CueTrackPositions/CueBlockNumber u 0-1 1 r=1..
0xea Cues/CuePoint/CueTrackPositions/CueCodecState u 0-1 2
0xdb Cues/CuePoint/CueTrackPositions/CueReference m 0-* 2
0x96 Cues/CuePoint/CueTrackPositions/CueReference/CueRefTime u 1-1 2
0x1941a469 Attachments m 0-1 1
0x61a7 Attachments/AttachedFile m 1-* 1
0x467e Attachments/AttachedFile/FileDescription 8 0-1 1
0x466e Attachments/AttachedFile/FileName 8 1-1 1
0x4660 Attachments/AttachedFile/FileMediaType s 1-1 1
0x465c Attachments/AttachedFile/FileData b 1-1 1
0x46ae Attachments/AttachedFile/FileUID u 1-1 1 r=1..
0x1043a770 Chapters m 0-1 1
0x45b9 Chapters/EditionEntry m 1-* 1
0x45bc Chapters/EditionEntry/EditionUID u 0-1 1 r=1..
0x45db Chapters/EditionEntry/EditionFlagDefault u 0-1 1 r=0..1
0x45dd Chapters/EditionEntry/EditionFlagOrdered u 0-1 1 r=0..1
0xb6 Chapters/EditionEntry/+ChapterAtom m 1-* 1
0x73c4 Chapters/EditionEntry/+ChapterAtom/ChapterUID u 1-1 1 r=1..
0x5654 Chapters/EditionEntry/+ChapterAtom/ChapterStringUID 8 0-1 3
0x91 Chapters/EditionEntry/+ChapterAtom/ChapterTimeStart u 1-1 1
0x92 Chapters/EditionEntry/+ChapterAtom/ChapterTimeEnd u 0-1 1
0x98 Chapters/EditionEntry/+ChapterAtom/ChapterFlagHidden u 0-1 1 r=0..1
0x6e67 Chapters/EditionEntry/+ChapterAtom/ChapterSegmentUUID b 0-1 1 l=16
0x6ebc Chapters/EditionEntry/+ChapterAtom/ChapterSegmentEditionUID u 0-1 1 r=1..
0x63c3 Chapters/EditionEntry/+ChapterAtom/ChapterPhysicalEquiv u 0-1 1
0x80 Chapters/EditionEntry/+ChapterAtom/ChapterDisplay m 0-* 1
0x85 Chapters/EditionEntry/+ChapterAtom/ChapterDisplay/ChapString 8 1-1 1
0x437c Chapters/EditionEntry/+ChapterAtom/ChapterDisplay/ChapLanguage s 0-* 1
0x437d Chapters/EditionEntry/+ChapterAtom/ChapterDisplay/ChapLanguageBCP47 s 0-* 4
0x437e Chapters/EditionEntry/+ChapterAtom/ChapterDisplay/ChapCountry s 0-* 1
0x6944 Chapters/EditionEntry/+ChapterAtom/ChapProcess m 0-* 1
0x6955 Chapters/EditionEntry/+ChapterAtom/ChapProcess/ChapProcessCodecID u 0-1 1
0x450d Chapters/EditionEntry/+ChapterAtom/ChapProcess/ChapProcessPrivate b 0-1 1
0x6911 Chapters/EditionEntry/+ChapterAtom/ChapProcess/ChapProcessCommand m 0-* 1
0x6922 Chapters/EditionEntry/+ChapterAtom/ChapProcess/ChapProcessCommand/ChapProcessTime u 1-1 1
0x6933 Chapters/EditionEntry/+ChapterAtom/ChapProcess/ChapProcessCommand/ChapProcessData b 1-1 1
0x1254c367 Tags m 0-* 1
0x7373 Tags/Tag m 1-* 1
0x63c0 Tags/Tag/Targets m 1-1 1
0x68ca Tags/Tag/Targets/TargetTypeValue u 0-1 1 r=1..
0x63ca Tags/Tag/Targets/TargetType s 0-1 1
0x63c5 Tags/Tag/Targets/TagTrackUID u 0-* 1
0x63c9 Tags/Tag/Targets/TagEditionUID u 0-* 1
0x63c4 Tags/Tag/Targets/TagChapterUID u 0-* 1
0x63c6 Tags/Tag/Targets/TagAttachmentUID u 0-* 1
0x67c8 Tags/Tag/+SimpleTag m 1-* 1
0x45a3 Tags/Tag/+SimpleTag/TagName 8 1-1 1
0x447a Tags/Tag/+SimpleTag/TagLanguage s 0-1 1
0x447b Tags/Tag/+SimpleTag/TagLanguageBCP47 s 0-1 4
0x4484 Tags/Tag/+SimpleTag/TagDefault u 0-1 1 r=0..1
0x4487 Tags/Tag/+SimpleTag/TagString 8 0-1 1
0x4485 Tags/Tag/+SimpleTag/TagBinary b 0-1 1
"""
Def = collections.namedtuple("Def", "name path parent kind least most minver low high open length nest")
SPAN = 1 << 20           # bytes of a master that check() reads whole. A larger one, as Attachments that hold fonts, it walks by
                         # the element headers, and it reads only the numbers it checks.
SEEKHEAD_MAX = 16 << 20  # bytes of a SeekHead it reads whole for its entries. An old mkvmerge lists every Cluster in one.
TOP_MAX = 64             # level-1 elements it reads in all
NOTES = 10               # notes kept per file
KINDS = ("version", "range", "schema", "framing", "tail_errors")   # the fault kinds check() finds
# The level-1 ids that ffmpeg's matroska_resync() looks for after a read error. It looks from the byte after the start of
# the last level-1 element it read.
RESYNC = (0x1549A966, 0x1654AE6B, 0x1C53BB6B, 0x1254C367, 0x114D9B74, 0x1941A469, 0x1F43B675, 0x1043A770)
# The EBML header elements of RFC 8794 that the check reads
EBML_HEAD = {0x4286: "EBMLVersion", 0x42F7: "EBMLReadVersion", 0x42F2: "EBMLMaxIDLength", 0x42F3: "EBMLMaxSizeLength", 0x4282: "DocType",
             0x4287: "DocTypeVersion", 0x4285: "DocTypeReadVersion", 0x4281: "DocTypeExtension"}


def parse(text):
    """({id: [Def]}, {parent path: [Def]}) of TABLE. A path starts at "Segment"."""
    by_id, children = {}, {}
    for line in filter(None, text.split("\n")):
        i, path, kind, occurs, minver, *more = line.split()
        opts = dict(x.split("=") for x in more)
        low, _, high = opts.get("r", "..").partition("..")
        least, _, most = occurs.partition("-")
        name, clean = path.rpartition("/")[2], "Segment/" + path.replace("+", "")
        d = Def(name.lstrip("+"), clean, clean.rpartition("/")[0], kind, int(least), None if most == "*" else int(most), int(minver),
                float(low.lstrip("(")) if low.lstrip("(") else None, float(high) if high else None, low.startswith("("),
                int(opts["l"]) if "l" in opts else None, name.startswith("+"))
        by_id.setdefault(int(i, 16), []).append(d)
        children.setdefault(d.parent, []).append(d)
    return by_id, children


SCHEMA, CHILDREN = parse(TABLE)


def short(path):
    """A path as a fault names it: "Tracks/TrackEntry/Video" for "Segment/Tracks/TrackEntry/Video"."""
    return path.partition("/")[2] or path


def wants(x):
    """The range of x as a fault says it: "over 0", "1 or more", "0 to 1"."""
    if x.high is None:
        return f"over {x.low:g}" if x.open else f"{x.low:g} or more"
    return f"{x.low:g} to {x.high:g}"


def number(b, kind):
    """The value of an uinteger (u), integer (i) or float (f) element. An empty one is 0."""
    if kind == "f":
        return struct.unpack(">f" if len(b) == 4 else ">d", b)[0] if b else 0.0
    return int.from_bytes(b, "big", signed=kind == "i")


class Walk:
    """The state of one check(): the bytes read, the faults by kind, the notes, and the minver of each element used."""

    def __init__(self, f, head):
        self.f, self.head, self.read, self.faults, self.notes, self.used = f, head, 0, {}, [], {}

    def bytes(self, pos, n):
        """n bytes of the file at pos. The first bytes that header_probe() read serve the ones they hold."""
        if pos + n <= len(self.head):
            return self.head[pos:pos + n]
        self.f.seek(pos); b = self.f.read(n); self.read += len(b)
        return b

    def fault(self, kind, text):
        if text not in self.faults.setdefault(kind, []):
            self.faults[kind].append(text)

    def note(self, text):
        if text not in self.notes and len(self.notes) < NOTES:
            self.notes.append(text)

    def master(self, rd, start, end, path):
        """Check the children of the master at path, which run from start to end. rd(pos, n) reads n bytes at pos.
        A child that does not read ends the walk of its parent."""
        counts, p, first = collections.Counter(), start, True
        while p < end:
            e = decide.element(rd(p, min(12, end - p)), 0)
            if not e or e[2] is None or p + e[1] + e[2] > end:
                why = "does not read" if not e else "has an unknown size" if e[2] is None else "runs past the end of its parent"
                return self.fault("framing", f"{short(path)}: the element at byte {p} {why} (RFC 8794 6)")
            cid, d, n = e[0], p + e[1], e[2]
            if cid == decide.CRC:   # it covers the bytes after it, up to the end of its parent
                if not first or n != 4:
                    self.fault("schema", f"{short(path)}: a CRC-32 that is not its first element of 4 bytes (RFC 9559 6.2)")
                elif end - d - 4 <= SPAN and int.from_bytes(rd(d, 4), "little") != zlib.crc32(rd(d + 4, end - d - 4)):
                    self.fault("schema", f"{short(path)}: its CRC-32 does not match (RFC 8794 11.3.1)")
            elif cid != decide.VOID:
                self.child(rd, cid, d, n, path, counts)
            first, p = False, d + n
        for c in CHILDREN.get(path, ()):
            if c.least and not counts[c.name]:
                self.fault("schema", f"{short(path)}: no {c.name}, which is mandatory (RFC 9559 5.1)")
            if c.most is not None and counts[c.name] > c.most:
                self.fault("schema", f"{short(path)}: {counts[c.name]} {c.name} elements, where RFC 9559 allows {c.most}")

    def child(self, rd, cid, d, n, path, counts):
        """Check the element cid in the master at path, with n bytes of data at d, and count it in counts."""
        here = [x for x in SCHEMA.get(cid, ()) if x.parent == path or (x.nest and x.path == path)]
        if not here:
            other = SCHEMA.get(cid)
            if other:   # a muxer wrote another id, as 0x80 for MuxingApp
                self.fault("schema", f"{short(path)}: {other[0].name} ({cid:#x}) sits there, and its place is {short(other[0].parent)} (RFC 9559 5.1)")
            else:   # a reader skips it (RFC 9559 7)
                self.note(f"{short(path)}: element {cid:#x}, which RFC 9559 does not define")
            return
        x = here[0]
        counts[x.name] += 1
        self.used[x.name] = x.minver
        if x.kind == "m":
            return self.master(rd, d, d + n, x.path)
        size = {"u": n <= 8, "i": n <= 8, "f": n in (0, 4, 8), "d": n in (0, 8)}.get(x.kind, True) and x.length in (None, n)
        if not size:
            return self.fault("range", f"{short(x.path)}: {n} bytes, a size its type does not allow (RFC 8794 7)")
        if x.kind in "uif" and (x.low is not None or x.high is not None):
            v = number(rd(d, n), x.kind)
            if (x.low is not None and (v <= x.low if x.open else v < x.low)) or (x.high is not None and v > x.high):
                self.fault("range", f"{short(x.path)} is {v:g}, and RFC 9559 wants {wants(x)}")

    def ebml(self, b):
        """The DocTypeVersion of the EBML header at the start of b, after its checks (RFC 9559 4.3, RFC 8794 11.2)."""
        e, got = decide.element(b, 0), {}
        for i, d, n in decide.children(b, e[1], e[1] + e[2]):
            if i == 0x4282:
                got[i] = b[d:d + n].rstrip(b"\0").decode("latin1")
            elif i in EBML_HEAD:
                got[i] = int.from_bytes(b[d:d + n], "big")
            elif i not in (decide.VOID, decide.CRC):
                self.note(f"EBML header: element {i:#x}, which RFC 8794 does not define")
        ver = got.get(0x4287, 1)
        if got.get(0x4282) not in ("matroska", "webm"):
            self.fault("schema", f"EBML header: DocType {got.get(0x4282)!r}, where RFC 9559 wants matroska (4.3)")
        if got.get(0x42F2, 4) != 4 or not 1 <= got.get(0x42F3, 8) <= 8:
            self.fault("schema", "EBML header: EBMLMaxIDLength must be 4 and EBMLMaxSizeLength 1 to 8 (RFC 9559 4.3)")
        if got.get(0x4285, 1) > ver:
            self.fault("version", f"EBML header: DocTypeReadVersion {got[0x4285]} is above DocTypeVersion {ver} (RFC 8794 11.2.6)")
        return ver


def check(f, head, ds, end, tail=0, cues=None):
    """(the RFC 9559 faults as {kind: [text]}, notes, bytes read past head) of the open Matroska file f. head holds
    its first bytes, ds is where the Segment data starts, and end where the Segment ends. tail counts the bytes past the
    Segment end. cues is (the file offset of the Cues data, those bytes) when the caller read them.

    It walks the level-1 elements from ds up to the first Cluster by their headers, and reads each one a SeekHead
    lists, Clusters left out. Each master it reads whole up to SPAN, else by its element headers. The kinds:
    framing, an element that does not read, has an unknown size, or runs past its parent. version, a DocTypeVersion
    below the minver of an element the file uses. range, a number out of its range or a size its type does not
    allow. schema, an element at another place than its own, a mandatory element missing, too many of one, a CRC-32
    that does not match, or a SeekHead entry where no such element starts. tail_errors, bytes past the Segment end
    after a last element that holds level-1 ids. ffmpeg then reads that element again from its second byte, and
    prints an error at each id it finds there. An element RFC 9559 does not define is a note, because a reader
    skips it (RFC 9559 7)."""
    w = Walk(f, head)
    ver, todo, found, p = w.ebml(head), [], {}, ds
    while p < end and len(todo) < TOP_MAX:   # the front, up to the first Cluster
        e = decide.element(w.bytes(p, 12), 0)
        if e and e[0] == decide.CLUSTER:   # a live stream may give a Cluster an unknown size (RFC 9559 5.1)
            break
        if not e or e[2] is None:
            why = "does not read" if not e else "has an unknown size"
            w.fault("framing", f"Segment: the element at byte {p}, before the first Cluster, {why} (RFC 8794 6)")
            break
        todo.append((p, e[0])); p += e[1] + e[2]
    for p, want in todo:   # the list grows as each SeekHead read lists more
        if p in found or len(found) >= TOP_MAX:
            continue
        e = decide.element(w.bytes(p, 12), 0)
        top = [x for x in SCHEMA.get(want, ()) if x.parent == "Segment"]
        name = top[0].name if top else f"{want:#x}"
        if e and e[0] == want and e[2] is None:
            w.fault("framing", f"Segment: the {name} at byte {p} has an unknown size (RFC 8794 6.2)")
            continue
        if not e or e[0] != want:
            if want != decide.CUES:   # the header check names lost Cues
                w.fault("schema", f"SeekHead: it lists {name} at byte {p - ds} of the Segment, and no {name} starts there (RFC 9559 6.3)")
            continue
        found[p] = e
        d, n = p + e[1], e[2]
        if d + n > end:
            w.fault("framing", f"Segment: the {name} at byte {p} runs past the end of the Segment (RFC 8794 6)")
            continue
        if want == decide.VOID:
            continue
        if not top:
            w.note(f"Segment: element {want:#x}, which RFC 9559 does not define")
            continue
        w.used[name] = top[0].minver
        whole = n <= SPAN or (want == decide.SEEKHEAD and n <= SEEKHEAD_MAX)
        buf = cues[1] if cues and cues[0] == d else w.bytes(d, n) if whole else None
        if buf is None and want == decide.CUES:   # Cues over CUES_MAX, which the header check does not read either
            continue
        rd = (lambda q, k, buf=buf, d=d: buf[q - d:q - d + k]) if buf is not None else w.bytes
        w.master(rd, d, d + n, top[0].path)
        if want == decide.SEEKHEAD and buf is not None:
            todo += [(ds + q, i) for i, qs in decide.seek_entries(buf, 0, len(buf), {}).items() if i != decide.CLUSTER for q in qs if ds + q < end]
    names = collections.Counter(next((x.name for x in SCHEMA.get(e[0], ()) if x.parent == "Segment"), None) for e in found.values())
    for c in CHILDREN["Segment"]:
        if c.least and not names[c.name]:
            w.fault("schema", f"Segment: no {c.name}, which is mandatory (RFC 9559 5.1)")
        if c.most is not None and names[c.name] > c.most:
            w.fault("schema", f"Segment: {names[c.name]} {c.name} elements, where RFC 9559 allows {c.most}")
    late = sorted(k for k, v in w.used.items() if v > ver)
    if late:
        w.fault("version", f"EBML header: DocTypeVersion {ver}, and the file uses {', '.join(late)} of version {max(w.used.values())} (RFC 9559 7)")
    p = max((q + e[1] + e[2] for q, e in found.items()), default=end)
    for _ in range(8):   # elements past the last one listed, as a SeekHead at the end that no other lists. A Cluster ends it.
        e = p < end and decide.element(w.bytes(p, 12), 0)
        if not e or e[2] is None or e[0] == decide.CLUSTER:
            break
        found[p], p = e, p + e[1] + e[2]
    last = next(((p, e) for p, e in found.items() if p + e[1] + e[2] == end), None)
    if tail and last and last[1][1] + last[1][2] <= SPAN:
        b = w.bytes(last[0], last[1][1] + last[1][2])
        if any(i.to_bytes(4, "big") in b[1:] for i in RESYNC):
            name = next((x.name for x in SCHEMA.get(last[1][0], ())), f"{last[1][0]:#x}")
            w.fault("tail_errors", f"the {tail} bytes past the Segment end make ffmpeg read the {name} before them again, and it prints errors there")
    return w.faults, w.notes, w.read
