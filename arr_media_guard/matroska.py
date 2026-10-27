# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The RFC 9559 check of the Matroska elements outside the Clusters (docs/design.md, "RFC 9559 check"), and the frame
reader (docs/design.md, "Frame times"). check() reads the EBML header, each level-1 element before the first Cluster,
and each one a SeekHead lists. It never reads a Cluster. It holds each element to the schema of RFC 9559 section 5.1
and to the EBML rules of RFC 8794. frames() reads the time of every frame from the blocks of the Clusters."""
import array, collections, contextlib, itertools, mmap, os, re, struct, threading, zlib

from . import decide, ff

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


# The frame reader (docs/design.md, "Frame times"). A block stores one time. A lace holds several frames, and RFC 9559
# 10.3.5 makes its frames contiguous, so frame k starts where frame k - 1 ends. The tag of a frame says where its time
# comes from.
STORED, DEFAULT, BITSTREAM, UNKNOWN = range(4)
TAGS = ("stored", "default", "bitstream", "unknown")
NONE = -(1 << 63)   # the time or the duration of a frame that is not known, in the arrays of frames()
NONZERO = re.compile(rb"[^\x00]")
CLUSTER_ID = b"\x1f\x43\xb6\x75"
TRACKENTRY, TRACKNUMBER, TRACKTYPE, CODECID, CODECPRIVATE, DEFAULTDURATION = 0xAE, 0xD7, 0x83, 0x86, 0x63A2, 0x23E383
ENCODINGS, ENCODING, SCOPE, ENCODING_TYPE, COMPRESSION, COMP_ALGO, COMP_SETTINGS = 0x6D80, 0x6240, 0x5032, 0x5033, 0x5034, 0x4254, 0x4255
AC3_RATES = (48000, 44100, 32000)
DTS_RATES = {1: 8000, 2: 16000, 3: 32000, 6: 11025, 7: 22050, 8: 44100, 11: 12000, 12: 24000, 13: 48000}
AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
GA_TYPES = (1, 2, 3, 4, 6, 7)   # the AAC object types aac_length() times, at 1024 or 960 samples a frame


def vint(b, p, end):
    """(length, value) of the EBML variable-size integer at p of b, which must end by byte end."""
    n = 9 - b[p].bit_length() if p < end else 9
    if n > 8 or p + n > end:
        raise ValueError(f"no variable-size integer at byte {p}")
    return n, int.from_bytes(b[p:p + n], "big") & ((1 << 7 * n) - 1)


def frame_length(codec, h):
    """(samples, sample rate) of the audio frame whose first bytes are h, from its header, or None. AC-3 and E-AC-3
    (ATSC A/52), DTS core (ETSI TS 102 114) and MPEG audio layers 1 to 3 carry both in each frame. An Opus packet
    carries its frame size and its count of frames in its TOC byte (RFC 6716 3.1), at 48 kHz."""
    if codec == "A_OPUS" and h:   # configs 0-11 SILK, 12-15 hybrid, 16-31 CELT, each with its frame sizes
        config, code = h[0] >> 3, h[0] & 3
        size = (480, 960, 1920, 2880)[config & 3] if config < 12 else (480, 960)[config & 1] if config < 16 else (120, 240, 480, 960)[config & 3]
        n = 1 if code == 0 else 2 if code < 3 else h[1] & 0x3F if len(h) > 1 else 0
        return (size * n, 48000) if n else None
    if codec.startswith(("A_AC3", "A_EAC3")) and len(h) >= 6 and h[:2] == b"\x0b\x77":
        bsid, fs, code = h[5] >> 3, h[4] >> 6, h[4] >> 4 & 3
        if bsid <= 10:   # AC-3: bsid 9 and 10 halve and quarter the rate
            return (1536, AC3_RATES[fs] >> max(0, bsid - 8)) if fs < 3 else None
        if bsid <= 16:   # E-AC-3: numblkscod, or fscod2 with 6 blocks
            return (1536, (24000, 22050, 16000)[code]) if fs == 3 and code < 3 else (256 * (1, 2, 3, 6)[code], AC3_RATES[fs]) if fs < 3 else None
    if codec.startswith("A_DTS") and len(h) >= 10 and h[:4] == b"\x7f\xfe\x80\x01":   # NBLKS, then SFREQ
        x = int.from_bytes(h[4:10], "big")
        return (((x >> 34 & 127) + 1) * 32, DTS_RATES[x >> 10 & 15]) if (x >> 10 & 15) in DTS_RATES else None
    if codec in ("A_MPEG/L1", "A_MPEG/L2", "A_MPEG/L3") and len(h) >= 3 and h[0] == 0xFF and h[1] >> 5 == 7:
        ver, layer, idx = h[1] >> 3 & 3, h[1] >> 1 & 3, h[2] >> 2 & 3   # version 3 is MPEG-1, 2 MPEG-2, 0 MPEG-2.5
        if ver != 1 and layer and idx < 3:
            return 384 if layer == 3 else 1152 if layer == 2 or ver == 3 else 576, (44100, 48000, 32000)[idx] >> (3 - ver if ver else 2)
    return None


def aac_length(private):
    """(samples, sample rate) of each frame of an AAC track from its AudioSpecificConfig (ISO/IEC 14496-3), the
    CodecPrivate, or None. The rate is the core rate, which an SBR stream doubles, so the frame time stays. Only the
    object types 1, 2, 3, 4, 6 and 7 count, or SBR and PS (5 and 29) over one of them. Each has 1024 samples a frame,
    or 960 when its frameLengthFlag is set. Every other type gives None. ER AAC LC (17) also has 1024 or 960 samples,
    and AAC-LD (23) has 512 or 480. The reader times no ER type, because no test file holds one."""
    x, n = int.from_bytes(private[:8], "big"), 8 * len(private[:8])
    bits = lambda p, k: (x >> n - p - k) & ((1 << k) - 1) if p + k <= n else None   # k bits at bit p
    aot, idx, p = bits(0, 5), bits(5, 4), 13   # the object type, the rate index, then 4 bits of channels
    if aot in (5, 29):   # SBR or PS: the extension rate index, its 24-bit rate when the index is 15, then the core type
        p += 28 if bits(p, 4) == 15 else 4
        aot, p = bits(p, 5), p + 5
    if aot not in GA_TYPES or idx is None or idx >= len(AAC_RATES):
        return None
    return 960 if bits(p, 1) else 1024, AAC_RATES[idx]   # the flag sits in the byte where the field before it ends


def vorbis_reader(private):
    """A function that takes the first byte of each Vorbis packet of a track in file order and gives its length in ns,
    or None when it is not known. private is the CodecPrivate: the three Vorbis headers, Xiph laced. A packet gives
    (the block size before it + its block size) / 4 samples (Vorbis I 1.3.2, 4.3.8). The block sizes come from the
    identification header. The mode of a packet, from its first byte, picks its block size. A long block names the
    size before it, and a short block takes the packet before. The mode table sits at the end of the setup header,
    after fields of variable size, so it reads backwards from the framing bit, as ffmpeg's vorbis_parser.c reads it.
    None when the headers do not read."""
    try:
        sizes, p = lace_sizes(private, 0, len(private), 0x02)
    except (ValueError, IndexError):
        return None
    ident, setup = private[p:p + sizes[0]], private[p + sum(sizes[:2]):] if len(sizes) == 3 else b""
    if ident[:7] != b"\x01vorbis" or setup[:7] != b"\x05vorbis" or len(ident) < 29:
        return None
    rate, bs = int.from_bytes(ident[12:16], "little"), (1 << (ident[28] & 15), 1 << (ident[28] >> 4))
    v, at = int.from_bytes(setup, "little"), len(setup) * 8 - 1   # bit k of the stream is bit k of v
    bits = lambda at, n: v >> (at - n + 1) & ((1 << n) - 1)   # n bits, read backwards from bit at
    while at >= 97 and not bits(at, 1):
        at -= 1
    start, count, modes = at - 1, 0, 0
    at = start
    while at >= 96:   # each mode backwards: mapping 8 bits, transform 16, window 16, blockflag 1
        if bits(at, 8) > 63 or bits(at - 8, 16) or bits(at - 24, 16):
            break
        at, count = at - 41, count + 1
        modes = count if bits(at, 6) + 1 == count else modes   # the mode count field before the modes
        if count > 63:
            break
    if not rate or not 0 < modes <= 63:
        return None
    flags = [bits(start - 40 - 41 * i, 1) for i in range(modes)][::-1]   # the blockflag of each mode, mode 0 first
    width, before = (modes - 1).bit_length(), [None]

    def step(x):
        mode = x >> 1 & ((1 << width) - 1)
        if x & 1 or mode >= modes:   # a header packet, or no mode
            before[0] = None
            return None
        cur = bs[flags[mode]]
        prev = bs[x >> (1 + width) & 1] if flags[mode] else before[0]
        before[0] = cur
        return None if prev is None else (prev + cur) // 4 * 1e9 / rate
    return step


def track_entry(b, start, end):
    """{number, type, codec, dd, private, strip, packed} of the TrackEntry data b[start:end]. dd is its DefaultDuration in
    ns or None. strip holds the bytes a header stripping took from the start of each frame. packed says another
    encoding changes each frame, so no frame header reads."""
    t = {"number": None, "type": None, "codec": "", "dd": None, "private": b"", "strip": b"", "packed": False}
    for i, d, s in decide.children(b, start, end):
        v = lambda: int.from_bytes(b[d:d + s], "big")
        if i == TRACKNUMBER: t["number"] = v()
        elif i == TRACKTYPE: t["type"] = v()
        elif i == CODECID: t["codec"] = bytes(b[d:d + s]).rstrip(b"\0").decode("ascii", "replace")
        elif i == DEFAULTDURATION: t["dd"] = v() or None
        elif i == CODECPRIVATE: t["private"] = bytes(b[d:d + s])
        elif i == ENCODINGS:
            for ei, ed, es in decide.children(b, d, d + s):
                got = {ci: (cd, cs) for ci, cd, cs in decide.children(b, ed, ed + es)} if ei == ENCODING else {}
                num = lambda k, default: int.from_bytes(b[got[k][0]:sum(got[k])], "big") if k in got else default
                if ei != ENCODING or not num(SCOPE, 1) & 1:   # scope 1: the frames
                    continue
                comp = {ci: (cd, cs) for ci, cd, cs in decide.children(b, got[COMPRESSION][0], sum(got[COMPRESSION]))} if COMPRESSION in got else {}
                comp_algo = int.from_bytes(b[comp[COMP_ALGO][0]:sum(comp[COMP_ALGO])], "big") if COMP_ALGO in comp else 0
                if num(ENCODING_TYPE, 0) == 0 and comp_algo == 3:
                    t["strip"] += bytes(b[comp[COMP_SETTINGS][0]:sum(comp[COMP_SETTINGS])]) if COMP_SETTINGS in comp else b""
                else:
                    t["packed"] = True
    return t


def lace_sizes(b, p, end, lacing):
    """(the byte sizes of the frames of the lace whose frame count sits at p of b, the byte of its first frame). The
    block ends at end. lacing is the lacing bits of the block flags: Xiph, EBML or fixed (RFC 9559 10.3). Raises
    ValueError when the sizes or the frames run past the end of the block."""
    n, p, sizes = b[p] + 1, p + 1, []
    if lacing == 0x04:
        if (end - p) % n:
            raise ValueError(f"a fixed-size lace at byte {p} does not split into {n} frames")
        return [(end - p) // n] * n, p
    if lacing == 0x02:
        for _ in range(n - 1):   # each size a run of bytes that ends at the first one under 255
            s = 0
            while True:
                if p >= end:
                    raise ValueError(f"the lace sizes at byte {p} run past the end of their block")
                s, p = s + b[p], p + 1
                if b[p - 1] != 255:
                    break
            sizes.append(s)
    else:
        k, s = vint(b, p, end)
        p += k
        sizes.append(s)
        for _ in range(n - 2):
            k, v = vint(b, p, end)
            p += k
            s += v - ((1 << 7 * k - 1) - 1)
            sizes.append(s)
    sizes.append(end - p - sum(sizes))
    if min(sizes) < 0:
        raise ValueError(f"the lace at byte {p} holds frames past the end of its block")
    return sizes, p


def lace_heads(b, sizes, p):
    """The first 16 bytes of each frame of a lace, or all its bytes when it is shorter, from lace_sizes() of the lace in
    b. frame_length() reads them."""
    return tuple(bytes(b[q:q + min(s, 16)]) for q, s in zip(itertools.accumulate(sizes, initial=p), sizes))


def heads_read(t):
    """Whether the length of each lace frame of the track entry t comes from the first bytes of the frame. That holds
    for an audio track with no DefaultDuration, no encoding that changes its frames, and no AAC config that gives the
    length."""
    return t["type"] == 2 and not t["packed"] and not t["dd"] and not (t["codec"].startswith("A_AAC") and aac_length(t["private"]))


def frames(path):
    """{"scale": the TimestampScale in ns, "tracks": [track_frames() of each TrackEntry the walk reads, one after damage
    too, in file order], "error": why the walk stopped, or None, "whole": True when every Cluster read whole, "resyncs": [(where the walk looked for a
    Cluster after damage, the Cluster it read on from)], "read_on": the tracks with the blocks read after each resync,
    or None with no resync, "end": the byte where the Segment ends, at most the size of the file} of the Matroska file
    path, from one walk of every Cluster (docs/design.md, "Frame times").
    It reads element headers, block headers and the sizes of every lace through mmap. Where a frame with no
    DefaultDuration needs the codec's frame length, it reads the first bytes of the frame in the same pass, or after it
    when Tracks comes after the Clusters. A second Info or Tracks does not count, as ffmpeg, mpv and mkvmerge read the
    first. It also reads the first bytes of the first frame of each audio track, see
    codec_frame in track_frames(). It skips a run of zero bytes before the first Cluster or after the last one,
    as an edit by mkvpropedit leaves one before the Tracks it moves to the end. An element or lace sizes that do not
    read, as in a damaged or cut Cluster, set error, and error says where. So do a zero run between two Clusters and a
    Cluster that runs past the end of the file. The walk then reads on from the next Cluster id, as ffmpeg does, see
    found(). A trusted Cluster reads whole, and its Timestamp is no later than the next Cluster that reads whole. The
    damaged Cluster the walk was in sets the lower bound of the search when its Timestamp lies between the last trusted
    Cluster and the next Cluster that reads whole after its end. Else the last trusted Cluster sets it, and the blocks
    of the damaged Cluster leave read_on. The walk reads on from the first Cluster that reads whole, no earlier than
    the bound and no later than the next Cluster that reads whole after it. A damaged Cluster between the damage and
    that Cluster keeps its blocks before its damage in read_on when its Timestamp lies between the two. With no Cluster
    that reads whole after it, the walk reads on from it only when the bytes after it read to the Segment end, see
    ends(). So a Cluster id inside damaged bytes gives no frame at a false time. A Segment that runs past the end of the file sets error after
    the walk. The tracks hold the blocks read before the first error, so a caller never takes them for the whole file,
    unless whole is True. The error is then a Segment size past the end of the file, or
    a level-1 element after the last Cluster whose size does not read, when no Cluster id follows it in the file, as
    one in a second file joined by cat. Each track of read_on holds resumed, the index of its first frame read after a
    resync. Raises ValueError when the file holds no Segment or a time past the range of track_frames(), and OSError
    when the file does not open."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    with LOCK:
        got = WALKS.pop(key, None)
    got = got or walk(path)
    with LOCK:
        WALKS[key] = got   # the walk used last goes last
        while len(WALKS) > 4:
            del WALKS[next(iter(WALKS))]
    return got


def carry(path, before):
    """Move the walk that frames() keeps for path from its os.stat() before an in-place mkvpropedit edit to its stat
    now. An edit of flags and languages changes no frame, and mkvpropedit moves no Cluster. Only a walk with no error
    of a Segment that ends at the end of the file moves. mkvpropedit puts bytes past the Segment end inside it, so a
    fresh walk can find damage there. It writes what it adds at the end of such a Segment, so the Segment still ends at
    the end of the file. Any other change of the file gives another stat, and the walk reads again."""
    with LOCK:
        got = WALKS.pop((path, before.st_size, before.st_mtime_ns), None)
    if got and got["error"] is None and got["end"] == before.st_size:
        with contextlib.suppress(OSError), LOCK:
            st = os.stat(path)
            WALKS[(path, st.st_size, st.st_mtime_ns)] = dict(got, end=st.st_size)


# ponytail: keyed by path, size and mtime, as subtitles.FULL is. A rewrite of the same size within one mtime tick (about
# 1 ms) reads the old walk. Add the inode or a hash of the last bytes to the key if a writer ever does that.
WALKS, LOCK = {}, threading.Lock()   # (path, size, mtime_ns): frames() of the last 4 files, as the proof and the header repair read the same files
# ponytail: a track whose first frames lie past HEAD_MAX, as audio that starts late, gives head() fewer frames than a
# whole walk. 500 AC-3 frames are 16 s, 240 MB at 120 Mbit/s. Raise it if a file needs more.
HEAD_MAX = 512 << 20   # bytes past the Segment start after which head() stops


def head(path, enough):
    """frames() of the first Clusters of path, or None. The walk that frames() keeps for the file serves when there is
    one. Else the walk stops after the first Cluster where enough(b, entries, blocks) holds, with b the bytes of the
    file, entries the track entries and blocks the blocks of cluster() so far. It also stops after the first Cluster
    that ends HEAD_MAX bytes or more past the Segment start, and at the first damage. error then says where it
    stopped. The tracks hold the blocks read before that, so their first frames are the first frames of a whole walk.
    Info and Tracks after the first Cluster, as mkvpropedit moves them, read where a SeekHead before it lists them, or
    a SeekHead that one lists. None when either one reads in neither place. The walk never goes into WALKS."""
    st = os.stat(path)
    with LOCK:
        got = WALKS.get((path, st.st_size, st.st_mtime_ns))
    return got or walk(path, enough)


def walk(path, enough=None):
    """frames() of path, or head() of it with enough."""
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as b:
        b.madvise(mmap.MADV_SEQUENTIAL)   # one pass from the start, so the pages read can go
        seg = decide.segment_start(b)
        if not seg:
            raise ValueError("the file holds no Matroska Segment")
        ds, seg_size = seg
        end = len(b) if seg_size is None else min(len(b), ds + seg_size)
        scale, entries, blocks, p, error, whole = 1000000, [], collections.defaultdict(list), ds, None, False
        clustered, hole = False, None   # whether a Cluster came yet, and the first zero run after one
        reads = {}   # the tracks whose frames keep their first bytes, see cluster()
        resyncs, cut = [], None   # (where each search starts, the Cluster the walk reads on from), the blocks of each track before the first damage
        last = pend = None   # the Timestamp of the last trusted Cluster, and of the last Cluster that read whole after it
        mark = None   # the count of blocks of each track before the Cluster the walk reads
        seen = set()   # the ids of the Info and the Tracks that top() read

        def top(i, d, s):   # read the Info or the Tracks whose data is b[d:d + s]
            nonlocal scale, entries, reads
            if i == decide.INFO:
                scale = next((int.from_bytes(b[cd:cd + cs], "big") for ci, cd, cs in decide.children(b, d, d + s) if ci == decide.TIMESTAMP_SCALE), scale)
            else:
                entries = [track_entry(b, cd, cd + cs) for ci, cd, cs in decide.children(b, d, d + s) if ci == TRACKENTRY]
                reads = {t["number"]: t["codec"] == "A_VORBIS" for t in entries if heads_read(t)}
            seen.add(i)
        while p < end:
            ff.check_stop()   # a walk of a large file on a slow NAS reads for minutes
            try:
                e = decide.element(b, p)
                if not e and not b[p]:   # a zero run that an edit left between level-1 elements, see checks.zero_gap()
                    m = NONZERO.search(b, p, end)
                    hole = p if clustered and hole is None else hole
                    p = m.start() if m else end
                    continue
                if not e or e[0] not in decide.LEVEL1:
                    raise damage(f"the Segment holds no level-1 element at byte {p}", p + 1)
                i, d, s = e
                if i == decide.CLUSTER:
                    if hole is not None:
                        raise damage(f"a run of zero bytes at byte {hole} lies between two Clusters", hole)
                    if enough and not clustered and {decide.INFO, decide.TRACKS} - seen:   # see head()
                        seek = decide.front_seeks(b, ds)
                        for q in seek.get(decide.SEEKHEAD, [])[:8]:   # as many as checks.seeks() reads
                            h = decide.element(b, ds + q)
                            if h and h[2] is not None:   # seek_entries() reads Seek children only
                                decide.seek_entries(b, h[1], min(end, h[1] + h[2]), seek)
                        for want in {decide.INFO, decide.TRACKS} - seen:
                            h = next((h for q in seek.get(want, []) if (h := decide.element(b, ds + q)) and h[0] == want and h[2] is not None
                                      and h[1] + h[2] <= end), None)
                            if not h:
                                return None
                            top(*h)
                    at, clustered, t, mark = p, True, first_time(b, d, end), {k: len(v) for k, v in blocks.items()}
                    p = cluster(b, d, end if s is None else min(end, d + s), s is None, blocks, reads)
                    if s is not None and d + s > len(b):
                        raise damage(f"the Cluster at byte {at} runs past the end of the file", len(b))
                    mark = None
                    if t is not None:   # a Cluster that reads whole trusts the one before when it comes no earlier
                        last, pend = pend if pend is not None and pend <= t else last, t
                    if enough and (p - ds >= HEAD_MAX or enough(b, entries, blocks)):
                        error = f"the walk of the first Clusters stops at byte {p}"
                        break
                    continue
                if s is None or d + s > end:   # every Cluster read whole when no Cluster id follows, in this file or one joined to it. head() skips the search.
                    whole = not enough and b.find(CLUSTER_ID, d) < 0
                    raise damage(f"the element at byte {p} has an unknown size or runs past the Segment end", d)
                if i in (decide.INFO, decide.TRACKS) and i not in seen:   # the first one counts, as ffmpeg and mkvmerge read it
                    top(i, d, s)
                p = d + s
            except ValueError as ex:   # damage or a cut file: the walk reads on from the next Cluster id, as ffmpeg does
                if error is None:
                    error, cut = str(ex), {k: v[:] for k, v in blocks.items()}
                if enough:   # see head()
                    break
                at = getattr(ex, "at", p + 1)   # past p, so the walk moves on
                t = t if mark is not None else None   # the Timestamp of the damaged Cluster the walk was in
                start = at if t is None or s is None else d + s   # every damage of a Cluster lies before its end
                n = after(b, start, end, last, reads)   # the next Cluster that reads whole after it
                last, pend = pend if pend is not None and n and pend <= n[1] else last, None
                fits = t is not None and (last is None or t >= last) and (not n or t <= n[1])
                if t is not None and not fits:
                    for k, v in blocks.items():   # the damaged Cluster at a false time keeps its blocks in tracks only
                        del v[mark.get(k, 0):]
                low = t if fits else last   # the earliest Timestamp a Cluster found after the damage may have
                w = n if start == at and not fits else after(b, at, end, low, reads)   # n holds for a later last too
                while w and (n := after(b, w[2], end, low, reads)) and w[1] > n[1]:   # a whole Cluster later than the next one
                    w = n
                if w and not n and not ends(b, w[2], end):   # no later Cluster agrees, and damage follows it
                    w = None
                if not w:
                    whole = whole and not resyncs
                    break
                q = b.find(CLUSTER_ID, at, w[0])
                while q >= 0:   # a damaged Cluster before w keeps its blocks before its damage when its Timestamp fits
                    f = found(b, q, end, low, reads)
                    keep = f and f[1] and f[2] is not None and f[0] <= w[1] and f[2] <= w[0]
                    if keep:
                        for k, v in f[1].items():
                            blocks[k] += v
                        resyncs.append((at, q))
                        at = f[2]
                    q = b.find(CLUSTER_ID, at if keep else q + 1, w[0])
                resyncs.append((at, w[0]))
                p, hole = w[0], None   # whole is False here: a Cluster id follows, see the level-1 branch
        if error is None and seg_size is not None and ds + seg_size > len(b):   # every Cluster read whole, so the frames are all there
            error, whole = f"the Segment runs {ds + seg_size - len(b)} bytes past the end of the file", True
        tracks = [track_frames(b, t, cut.get(t["number"], []) if cut is not None else blocks.get(t["number"], ()), scale)
                  for t in entries]
        read_on = [dict(track_frames(b, t, blocks.get(t["number"], ()), scale), resumed=len(c["time"])) for t, c in zip(entries, tracks)] if resyncs else None
        return {"scale": scale, "tracks": tracks, "error": error, "whole": error is None or whole, "resyncs": resyncs, "read_on": read_on,
                "end": end}


def first_time(b, d, end):
    """The Timestamp of the Cluster whose data starts at byte d of b, when it is the first child past a CRC-32 and a
    Void, else None."""
    e = decide.element(b, d)
    while e and e[0] in (decide.CRC, decide.VOID) and e[2] is not None and e[1] + e[2] < end:
        e = decide.element(b, e[1] + e[2])
    return int.from_bytes(b[e[1]:e[1] + e[2]], "big") if e and e[0] == decide.TIMESTAMP and e[2] is not None else None


def found(b, p, end, last, reads):
    """(its Timestamp, its blocks, the byte of its damage or None, where it ends or None) of the Cluster whose id sits
    at byte p of b, found after damage, or None. Damaged bytes can hold a Cluster id whose children read for a while,
    as a frame at a wrong time. So the Cluster counts only when its first child is a Timestamp no earlier than last,
    the lower bound of the search, see frames(). The blocks are the ones before its damage, see cluster()."""
    e = decide.element(b, p)
    if not e or e[0] != decide.CLUSTER:
        return None
    d, s = e[1], e[2]
    t = first_time(b, d, end)
    if t is None or (last is not None and t < last):
        return None
    got = collections.defaultdict(list)
    try:
        return t, got, None, cluster(b, d, end if s is None else min(end, d + s), s is None, got, reads)
    except ValueError as ex:
        return t, got, ex.at, None


def after(b, q, end, last, reads):
    """(its byte, its Timestamp, where it ends) of the first Cluster id from byte q of b whose Cluster found() counts
    and reads whole, or None."""
    q = b.find(CLUSTER_ID, q, end)
    while q >= 0:
        f = found(b, q, end, last, reads)
        if f and f[2] is None:
            return q, f[0], f[3]
        q = b.find(CLUSTER_ID, q + 1, end)
    return None


def ends(b, q, end):
    """Whether the bytes b[q:end] read as level-1 elements up to end, as after the last Cluster of a file. A zero run
    reads, and so does a last element that runs past end or has an unknown size, as in a cut file. Fewer than 12 bytes
    left to the end of the file read as the header of an element cut there, as in decide.cluster_blocks()."""
    while q < end:
        e = decide.element(b, q)
        if not e and not b[q]:
            m = NONZERO.search(b, q, end)
            q = m.start() if m else end
            continue
        if not e and len(b) - q < 12:   # an id and a size take at most 12 bytes
            return True
        if not e or e[0] not in decide.LEVEL1:
            return False
        q = end if e[2] is None else e[1] + e[2]
    return True


def damage(text, at):
    """A ValueError with the text of a damage the walk finds. The walk looks for the next Cluster id from byte at."""
    ex = ValueError(text)
    ex.at = at
    return ex


def cluster(b, start, end, open_, blocks, reads):
    """Add each block of the Cluster data b[start:end] to blocks, {track number: [(its time in ticks, its frames, its
    byte, the byte after its flags, its end, its lacing bits, its BlockDuration in ticks or None, its empty frames, its
    frame heads)]}. Returns where the Cluster ends. A Cluster of unknown size, open_, ends at the next level-1 element.
    Raises ValueError when a child or the sizes of a lace do not read. The empty frames are a mask with bit k set when
    frame k of a lace holds 0 bytes. reads maps the number of each track that keeps the first
    bytes of its frames to True for Vorbis. The frame heads are lace_heads() of a lace of a track in reads, the first
    byte of an unlaced block of a Vorbis track in reads, else None. vorbis_reader() takes the mode of every packet in
    turn, unlaced ones too. It reads the lace sizes and the frame heads while the block is in memory. After the walk,
    MADV_SEQUENTIAL has dropped the pages, and a read of each lace faults its page in again. The ValueError holds in at
    the byte of the child that does not read, see damage()."""
    ts, q = None, start
    try:
        while q < end:
            e = decide.element(b, q)
            if e and open_ and e[0] in decide.LEVEL1 and e[0] not in decide.IN_CLUSTER:
                return q
            if not e or e[2] is None or e[0] not in decide.IN_CLUSTER or e[1] + e[2] > end:
                raise ValueError(f"the Cluster at byte {start} holds no Cluster child at byte {q}")
            i, d, s = e
            if i == decide.TIMESTAMP:
                ts = int.from_bytes(b[d:d + s], "big")
            elif i in (decide.SIMPLEBLOCK, decide.BLOCKGROUP):
                bd, bs, dur = d, s, None
                if i == decide.BLOCKGROUP:
                    got = {gi: (gd, gs) for gi, gd, gs in decide.children(b, d, d + s)}
                    if decide.BLOCK not in got:
                        raise ValueError(f"the BlockGroup at byte {q} holds no Block")
                    (bd, bs), du = got[decide.BLOCK], got.get(decide.BLOCKDURATION)
                    dur = int.from_bytes(b[du[0]:du[0] + du[1]], "big") if du else None
                if ts is None:
                    raise ValueError(f"the block at byte {q} comes before the Timestamp of its Cluster")
                n, track = vint(b, bd, bd + bs)
                head = bd + n + 3   # the track number, the relative time and the flags
                lacing = bs >= n + 3 and b[head - 1] & 0x06
                if bs < n + (4 if lacing else 3):
                    raise ValueError(f"the block at byte {q} is shorter than its header")
                rel = int.from_bytes(b[bd + n:bd + n + 2], "big", signed=True)
                empty, heads = 0, None
                if lacing:
                    sizes, p = lace_sizes(b, head, bd + bs, lacing)
                    empty = sum(1 << k for k, size in enumerate(sizes) if not size)
                    heads = lace_heads(b, sizes, p) if track in reads else None
                elif reads.get(track):
                    heads = b[head:min(head + 1, bd + bs)]
                blocks[track].append((ts + rel, b[head] + 1 if lacing else 1, q, head, bd + bs, lacing, dur, empty, heads))
            q = d + s
    except ValueError as ex:
        ex.at = q
        raise
    return end


def track_frames(b, t, blocks, scale):
    """The track entry t with the frames of its blocks, see cluster(). Each list holds one item per frame in file
    order: time (ns, NONE when not known), tag (an index of TAGS), place (the byte of its block), dur (ns, NONE when not
    known) and full (1 when the frame holds data). A frame of a packed text track holds no data when its zlib stream
    ends with no byte out. ffmpeg, mkvextract and the Cues read drop it, as they drop a frame of 0 bytes. frame is the
    ns of one frame: the DefaultDuration, else the length the codec gives the first frame, else None. codec_frame is
    the ns of one frame as the codec defines it for AAC, AC-3, E-AC-3, DTS and MPEG audio, else None. It comes from the
    AAC config or the header of the first frame, whatever the DefaultDuration says. overlaps and gaps are [count, the
    worst ns, the first time in ns] of the audio blocks that start more than a tick before the end of the block before,
    or after it. lapped holds the time in ns of each such block that starts before that end where the block before is
    a lace. The times of a lace's frames come from its block time and their lengths, so they can run past the next
    block, see remux.planned_times().

    A frame's time is stored for an unlaced block and frame 0 of a lace. Frame k of a lace is default at the block time
    plus k DefaultDurations (RFC 9559 10.3.5), else bitstream at the block time plus the lengths of the frames before
    it, from the codec, see frame_length() and aac_length(), else unknown. A frame's duration is its BlockDuration, else
    the DefaultDuration, else for a lace frame its codec length, else NONE. RFC 9559 then ends an unlaced block at the
    next block in time order, see subtitles.timed(). Raises ValueError when a time or a duration in ns does not fit a
    signed 64-bit integer. libmkv 0.6.5 wrote a Cluster Timestamp of -16 ms as 2^64 ns less 16 ms, in ticks."""
    dd, codec = t["dd"], t["codec"]
    fixed = aac_length(t["private"]) if codec.startswith("A_AAC") and not t["packed"] else None
    readable = t["type"] == 2 and not t["packed"] and not dd
    vorbis = vorbis_reader(t["private"]) if codec == "A_VORBIS" and readable else None   # it keeps the block size before
    text = t["type"] == 17 and t["packed"] and codec.startswith("S_TEXT/")

    def holds(p, end):   # whether the unlaced frame b[p:end] holds data. An inflate stops at the first byte out.
        if not (text and end > p):
            return end > p
        z = zlib.decompressobj()
        try:
            return bool(z.decompress(b[p:end], 1)) or not z.eof   # a damaged stream counts as data, so the proof refuses
        except zlib.error:
            return True

    def length(h):   # the ns of a frame whose first bytes are h, from its codec, or None
        if vorbis:
            head = t["strip"] + h[:1]
            return vorbis(head[0]) if head else None
        got = fixed or (frame_length(codec, t["strip"] + h) if readable else None)
        return got and got[0] * 1e9 / got[1]

    first = lambda: blocks[0][3] if blocks[0][1] == 1 else lace_sizes(b, *blocks[0][3:6])[1]   # the first frame of the first block
    frame = length(bytes(b[first():first() + 16])) if blocks and readable and not vorbis else dd
    own = fixed or (frame_length(codec, t["strip"] + bytes(b[first():first() + 16])) if blocks and t["type"] == 2 and not t["packed"]
                    and codec.startswith(("A_AC3", "A_EAC3", "A_DTS", "A_MPEG/L")) else None)
    time, tag, place, dur, full = array.array("q"), bytearray(), array.array("q"), array.array("q"), bytearray()
    over, gap, last, laced, lapped = [0, 0, None], [0, 0, None], None, False, array.array("q")
    try:
        for i, (tk, n, pos, head, end, lacing, bdur, empty, heads) in enumerate(blocks):
            if not i & 0xffff:   # a TrueHD track of a film holds millions of blocks
                ff.check_stop()
            t0 = tk * scale
            if n == 1:
                d = bdur * scale if bdur is not None else dd or NONE
                time.append(t0); tag.append(STORED); place.append(pos); dur.append(d); full.append(holds(head, end))
                span = d if d != NONE else frame
                if vorbis:   # the next packet's length takes this one's block size
                    length(heads if heads is not None else b[head:min(head + 1, end)])
            else:
                lens = None
                if fixed and not dd:   # the AAC config gives the length of every frame
                    lens = [length(b"")] * n
                elif readable:   # cluster() kept the heads, unless Tracks came after the Clusters
                    lens = [length(h) for h in heads or lace_heads(b, *lace_sizes(b, head, end, lacing))]
                    lens = None if None in lens else lens
                at = 0.0
                for k in range(n):
                    time.append(t0 if not k else t0 + k * dd if dd else t0 + round(at) if lens else NONE)
                    tag.append(STORED if not k else DEFAULT if dd else BITSTREAM if lens else UNKNOWN)
                    dur.append(dd or (round(lens[k]) if lens else NONE))
                    place.append(pos); full.append(not empty >> k & 1)
                    at += lens[k] if lens else 0
                span = n * dd if dd else round(at) if lens else (bdur * scale if bdur is not None else None)
            if t["type"] == 2 and last is not None:   # audio blocks against the end of the block before
                for x, off in ((over, last - t0), (gap, t0 - last)):
                    if off > scale:
                        x[0], x[1], x[2] = x[0] + 1, max(x[1], off), x[2] if x[2] is not None else t0
                        if x is over and laced:
                            lapped.append(t0)
            last, laced = t0 + span if span else None, n > 1
    except OverflowError:   # the arrays hold signed 64-bit ns, as libmatroska and ffmpeg hold times
        raise ValueError(f"the block at byte {pos} of track {t['number']} stores a time or a duration of 2^63 ns or more") from None
    return dict(t, time=time, tag=tag, place=place, dur=dur, full=full, frame=frame, codec_frame=own and own[0] * 1e9 / own[1], overlaps=over,
                gaps=gap, lapped=lapped)


def rfc_ends(blocks):
    """[the end of each block, or None] of blocks [(start, duration or None)], in any one unit. A block with no duration ends
    where the next block in time order starts, as RFC 9559 defines the duration of a block with no BlockDuration and
    no DefaultDuration. The last such block has no known end, None."""
    order = sorted(range(len(blocks)), key=lambda k: blocks[k][0])
    after = {order[i]: blocks[order[i + 1]][0] for i in range(len(order) - 1)}
    return [s + d if d is not None else after.get(k) for k, (s, d) in enumerate(blocks)]


def rfc_cues(blocks):
    """[(start, end or None, data)] of the subtitle blocks [(start, duration or None, data)] in their order, with the
    ends of rfc_ends(). A block whose data is None holds an empty frame. It gives no cue, as ffmpeg, mkvextract and
    mkvmerge drop it. It still ends a block with no duration before it, so that cue keeps its end."""
    ends = rfc_ends([(s, d) for s, d, _ in blocks])
    return [(s, e, x) for (s, _, x), e in zip(blocks, ends) if x is not None]


def block_data(f, at, number, head=False, packed=False, most=64 << 10):
    """(the frame data, decide.block_times()) of the unlaced block of track number at byte at of the open file f, or
    (None, None) when no whole block of that track starts there. A block over most bytes reads in part. With head, only
    the first bytes read, as a PGS block can hold a large picture, see decide.block_head(). packed says zlib compresses
    the frame, and a block read in part decompresses in part."""
    f.seek(at); blk = f.read(1024); e = decide.element(blk, 0)
    if not head and e and e[2] is not None and len(blk) < e[1] + e[2] <= most:
        blk += f.read(e[1] + e[2] - len(blk))
    frame, times = decide.block_head(blk, number) if head else (decide.block_frame(blk, number), decide.block_times(blk))
    return (None, None) if frame is None else (zlib.decompressobj().decompress(frame) if packed else frame, times)
