#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The burned-in subtitle check of arr-media-guard. It finds dialogue subtitles drawn into the picture of a file.

A burned-in subtitle is part of the video frames, so no track flag turns it off. Subtitles show while people speak.
So the check compares keyframes inside speech with keyframes in the silence between. A file with burned-in dialogue
has subtitle lines in most speech frames and in few silence frames. A logo or a clock shows in both.

- quick() sorts a file at import into clean, flagged or unsure, and changes nothing. Silero VAD reads 10 windows of
  30 seconds of the audio. Up to 40 keyframes in speech and 40 in silence inside them go through method B. Method B
  is a stroke test on the luma and needs no model. A file with under MIN_SPEECH_FRAMES speech keyframes gets more
  windows, and then it is unsure. Method B misses white text on HDR video, so HDR video that it finds clean is unsure.
  A Matroska file without cues is unsure at once, because each seek in it reads the file up to the time.
- full() decides. lid.speech() gives the speech of the whole file, from its cache when another check read it. Up to
  40 keyframes in speech and 40 in silence go through method A. The PP-OCRv4 text detector finds the lines of text.
  A line with the shape and place of a subtitle counts, unless it sits in the same place in many frames (an overlay).
  A label needs three things. Enough speech frames have a subtitle line. That share lifts over the share of the
  silence frames. The hits spread over enough tenths of the file. Under MIN_SPEECH_FRAMES speech frames the label is
  unsure. Two recognizers read the lines of up to 10 frames for the script and the language.
- ready() checks the models. fetch() downloads them at install, never at run time.

A keyframe needs no other frame to decode, so a sample costs one seek and one decode. Each candidate time seeks to the
keyframe at or before it and reads that packet. The packet decodes only when its time falls in the class it is
wanted for. Frame times run on the file's clock, from its start, as the speech times do. The thresholds in LIMITS
and METHOD_B come from a study on verified files, frozen before the score.

numpy, onnxruntime and PyAV load only inside quick() and full(), in the venv of lid.py. The hook runs this module as a
subprocess of that venv's Python, as it runs lid.py. The CLI prints one JSON line:

    /opt/arr-media-guard-lid/venv/bin/python burnin.py PATH INDEX DURATION --quick
    /opt/arr-media-guard-lid/venv/bin/python burnin.py PATH INDEX DURATION --full [--second] --cache FILE --model-dir DIR

At install, "burnin.py --fetch --model-dir DIR" downloads the pinned models once and checks each sha256.
"""
import argparse, bisect, hashlib, json, os, sys, time, urllib.request

if __name__ == "__main__":
    # Run by path in the venv, as lid.py runs. The folder above the package takes the place of the package folder on
    # sys.path, so the package's modules import and none of them hides a module of the venv.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    __package__ = "arr_media_guard"
from . import lid  # noqa: E402

THREADS = 1                       # one thread each for the decoder and the models. More cost more CPU than they save in wall time.
N = 40                            # keyframes per class, speech and silence. With 10 or 20 the study called full burn-ins wrong.
QUICK_WINDOWS = 10                # audio windows of quick(), centred at (k + 0.5) / QUICK_WINDOWS of the duration
MIN_SPEECH_FRAMES = 10            # speech frames a check needs. With fewer, quick() adds the windows between, then it is
                                  # unsure. full() is unsure at once.
FLAG = 0.05                       # quick() flags a file whose method B speech share reaches this
# The models: PaddleOCR's PP-OCRv4 mobile text detector, its Chinese and English recognizer, and the PP-OCRv3 English
# recognizer, as ONNX files from RapidOCR (Apache-2.0, see NOTICE). The Chinese recognizer joins some words of Latin
# text, so the English one reads Latin lines again. The language counts words.
MODEL_URL = "https://huggingface.co/SWHL/RapidOCR/resolve/1cfba2e90fc938db55889873735088de210cc173/"
MODELS = {   # name -> (path under MODEL_URL, sha256)
    "det": ("PP-OCRv4/ch_PP-OCRv4_det_infer.onnx", "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"),
    "ch": ("PP-OCRv4/ch_PP-OCRv4_rec_infer.onnx", "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b"),
    "en": ("PP-OCRv3/en_PP-OCRv3_rec_infer.onnx", "ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea"),
}
MODEL_SUB = "ocr"                 # the models sit in MODEL_DIR/ocr, beside lid.py's Whisper model
# Method A. Shares are of the frame width or height. Heights are of the detector's text core, which is smaller
# than the letters.
LIMITS = {
    "det_width": 960,      # frames scale down to this width for the detector, never up
    "det_bin": 0.3,        # the detector's probability map turns binary at this, PaddleOCR's own default
    "box_score": 0.6,      # mean probability inside a line box, PaddleOCR's box_thresh
    "unclip": 1.5,         # the detector marks a shrunk core of each line. PaddleOCR's unclip_ratio grows it back to
                           # the letters before a recognizer reads it.
    "cand": 4,             # candidate times per wanted frame. A keyframe outside its class costs a seek and no decode.
    "speech_pad": 0.3,     # a speech keyframe sits this far inside a span of speech
    "sil_gap": 2.0,        # a silence keyframe sits in a gap of speech this long or longer
    "sil_pad": 0.8,        # and this far from any speech
    "max_packets": 4000,   # packets one candidate may read before it gives up
    "centre": 0.12,        # a subtitle line's centre sits this close to the middle of the width
    "min_h": 0.018,        # line height. Subtitles were 0.020-0.033 in the study.
    "max_h": 0.06,
    "min_aspect": 3.0,     # width over height of a line. Subtitles were 6 or more, jersey numbers and signs 1.9-2.7.
    "min_w": 0.04,         # line width. A ticker spans the frame.
    "max_w": 0.92,
    "bottom": 0.70,        # a bottom line has its centre below this share of the height
    "top": 0.16,           # a top line has its centre above this share
    "max_lines": 3,        # more centred lines in one band are credits or a title card
    "crowd_mid": 2,        # a frame with this many centred lines between the bands, or crowd_all in all, is credits
    "crowd_all": 5,
    "overlay_tol": 0.015,  # a box whose four edges recur within this
    "overlay_share": 0.25,  # in this share of all frames is an overlay, such as a clock or a channel logo
    "full": 0.40,          # the share of speech frames with a subtitle line for "full"
    "partial": 0.08,       # and for "partial"
    "lift": 0.10,          # speech share less silence share, at least, for either label
    "full_spread": 4,      # tenths of the file the speech hits spread over, for "full". Credits bunch up in time.
    "partial_spread": 2,   # and for "partial"
    "rec_frames": 10,      # speech frames whose subtitle lines the recognizers read
    "rec_conf": 0.6,       # the mean character probability of a line that counts as read
    "lang_letters": 80,    # letters the language needs
    "lang_cover": 0.25,    # the share of the words that the English word list must hold
    "lang_ratio": 1.5,     # and this many times the share of the next language's list
    "not_eng": 0.15,       # an English share under this, with enough letters, is text in another language
    "unread": 0.3,         # with under this share of 3 or more lines read, the text is in a script the recognizers lack
}
# Method B: subtitle strokes are short bright runs with a darker pixel close on both sides. On a cartoon outline the
# polarity is the reverse, and a bright area is a long run.
METHOD_B = {
    "width": 720,          # luma frames scale down to this width, never up
    "bottom": 0.68,        # the bottom band starts at this share of the height
    "top": 0.18,           # the top band ends at this share
    "min_h": 0.015,        # line height, share of the frame height
    "max_h": 0.09,
    "centre": 0.12,        # as LIMITS
    "min_w": 0.05,
    "max_w": 0.92,
    "fill": 150,           # luma of a stroke pixel, at least
    "edge": 90,            # a darker pixel this far under the stroke's brightest within 0.008 of the height on each side
}
# FFmpeg's numbers for the PQ (SMPTE ST 2084) and HLG (ARIB STD-B67) transfers of HDR video. White text on PQ video
# reads 134-149 in 8-bit luma, under METHOD_B["fill"], so quick() sends such a file to full() when B finds it clean.
HDR_TRC = (16, 18)


def quantile_points(spans, n, pad):
    """n times spread evenly over the summed length of spans, each at least pad inside its span."""
    spans = [(a + pad, b - pad) for a, b in spans if b - a > 2 * pad]
    total = sum(b - a for a, b in spans)
    if not spans or n <= 0:
        return []
    out, acc, k = [], 0.0, 0
    for a, b in spans:
        while k < n and (k + 0.5) / n * total <= acc + (b - a):
            out.append(round(a + (k + 0.5) / n * total - acc, 3))
            k += 1
        acc += b - a
    return out


def spread(n):
    """0..n-1 in bit-reversal order, so every prefix spreads over the range. A run that stops early still covers the file."""
    bits = max(1, (n - 1).bit_length())
    return [i for i in sorted(range(1 << bits), key=lambda x: int(f"{x:0{bits}b}"[::-1], 2)) if i < n]


def gaps_of(spans, start, end):
    """The gaps of LIMITS["sil_gap"] or more between spans from start to end, as (start, end)."""
    gaps, last = [], start
    for a, b in [*spans, [end, end]]:
        if a - last >= LIMITS["sil_gap"]:
            gaps.append((last, a))
        last = b
    return gaps


class Sampler:
    """One video file open in PyAV. keyframe() reads the keyframe packet at or before a time with no decode, and
    decode() decodes a packet that is kept. Times run on the file's clock: a stream time less the container's start
    time, as lid.speech() and an ffmpeg -ss give them. A TS recording or a remux that kept its times starts late."""

    def __init__(self, path, threads=THREADS):
        import av
        self.av = av
        self.c = av.open(path)
        self.v = next((v for v in self.c.streams.video if not v.disposition & av.stream.Disposition.attached_pic), None)
        if self.v is None:   # cover art is no video
            self.c.close()
            raise ValueError("the file has no video stream")
        self.start = (self.c.start_time or 0) / 1e6
        self.hdr = self.v.codec_context.color_trc in HDR_TRC
        self.v.thread_type = "AUTO"
        self.v.codec_context.thread_count = threads
        self.v.codec_context.options = {"skip_loop_filter": "all"}   # the same text for less decode work
        w, h = self.v.codec_context.width, self.v.codec_context.height
        sc = min(1.0, LIMITS["det_width"] / w)
        self.size = (max(32, int(round(w * sc / 32)) * 32), max(32, int(round(h * sc / 32)) * 32))   # the detector takes multiples of 32

    def close(self):
        self.c.close()

    def no_cues(self, duration):
        """True for a Matroska file with no cues: the index of its video ends before half of duration. A seek in such a
        file reads it up to the time, and each ffmpeg -ss reads it from the start. The demuxer reads the cues at the
        first seek, so a seek to the start comes first. It reads only the first cluster of a file with no cues."""
        if "matroska" not in self.c.format.name:
            return False
        self.c.seek(0)
        e = self.v.index_entries
        return not len(e) or float(e[-1].timestamp * self.v.time_base) - self.start < duration / 2

    def keyframe(self, t):
        """(time, packet) of the keyframe at or before t, or (None, None). A TS file has no index, so its seek lands on
        any packet, and the next keyframe comes back."""
        self.c.seek(int(max(0.0, t + self.start) * self.av.time_base), backward=True, any_frame=False)
        self.v.codec_context.flush_buffers()
        for n, p in enumerate(self.c.demux(self.v)):
            if n > LIMITS["max_packets"]:
                break
            ts = p.pts if p.pts is not None else p.dts
            if p.is_keyframe and ts is not None:
                return round(float(ts * self.v.time_base) - self.start, 3), p
        return None, None

    def frame_at(self, t):
        """(time, frame) of the first frame at or after t that is no keyframe, decoded from the keyframe before t, or
        (None, None)."""
        self.c.seek(int(max(0.0, t + self.start) * self.av.time_base), backward=True, any_frame=False)
        self.v.codec_context.flush_buffers()
        self.v.codec_context.skip_frame = "DEFAULT"
        for n, f in enumerate(self.c.decode(self.v)):
            if n > LIMITS["max_packets"]:
                break
            if f.time is not None and f.time - self.start >= t and not f.key_frame:
                return round(f.time - self.start, 3), f
        return None, None

    def decode(self, p, rgb):
        """The picture of keyframe packet p, or of a decoded frame: RGB at the detector's size when rgb, else luma
        METHOD_B["width"] wide. None when it does not decode."""
        cc = self.v.codec_context
        cc.skip_frame = "DEFAULT"
        try:
            fs = [p] if isinstance(p, self.av.VideoFrame) else list(cc.decode(p)) + list(cc.decode(None))
        except self.av.error.FFmpegError:
            return None
        if not fs:
            return None
        if rgb:
            return fs[0].to_ndarray(format="rgb24", width=self.size[0], height=self.size[1])
        sc = min(1.0, METHOD_B["width"] / cc.width)
        return fs[0].to_ndarray(format="gray", width=int(cc.width * sc) // 2 * 2, height=int(cc.height * sc) // 2 * 2)


def sample(sm, want, kind_of, look, exact=False, stop=None):
    """[{"kind", "at", "look": look(sm, a packet or a frame, its time)}] for up to N frames of each kind in want, {kind:
    candidate times}. The candidates go in spread() order. Each takes the keyframe at or before it. With exact it takes
    the first frame at or after it that is no keyframe. A frame counts once, and only when kind_of(its time) is its
    kind. look() gives None for a frame that does not decode. stop() is asked after every 10 frames, and a True ends
    the sampling with None."""
    frames, seen = [], set()
    for kind, times in want.items():
        got = 0
        for i in spread(len(times)):
            if got >= N:
                break
            try:
                k, pk = (sm.frame_at if exact else sm.keyframe)(times[i])
            except Exception:
                continue
            if k is None or k in seen or kind_of(k) != kind:
                continue
            x = look(sm, pk, k)
            if x is None:
                continue
            seen.add(k)
            frames.append({"kind": kind, "at": k, "look": x})
            got += 1
            if stop and len(frames) % 10 == 0 and stop():
                return None
    return frames


# --- method A: the text detector -------------------------------------------------------------------------------------

class Ocr:
    """The detector and the two recognizers, on the CPU with THREADS threads."""

    def __init__(self, model_dir, threads=THREADS):
        import numpy as np
        import onnxruntime as ort
        self.np = np
        o = ort.SessionOptions()
        o.intra_op_num_threads, o.inter_op_num_threads = threads, 1
        o.log_severity_level = 3
        o.add_session_config_entry("session.intra_op.allow_spinning", "0")   # no busy wait on a shared host
        path = lambda name: os.path.join(model_dir, MODEL_SUB, os.path.basename(MODELS[name][0]))
        self.det = ort.InferenceSession(path("det"), o, providers=["CPUExecutionProvider"])
        self.rec, self.chars = {}, {}
        for k in ("ch", "en"):
            self.rec[k] = ort.InferenceSession(path(k), o, providers=["CPUExecutionProvider"])
            # The English list ends in a newline. Without the strip, the class of the space reads as an empty string.
            self.chars[k] = ["<blank>"] + self.rec[k].get_modelmeta().custom_metadata_map["character"].rstrip("\n").split("\n") + [" "]

    def boxes(self, rgb):
        """The text line boxes of one frame, see line_boxes()."""
        np = self.np
        x = (rgb.astype(np.float32) / 255.0 - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        return line_boxes(self.det.run(None, {"x": x.transpose(2, 0, 1)[None]})[0][0, 0])

    def read(self, rgb, box, model="ch"):
        """(text, mean character probability) of the line in box, read by the recognizer model. The crop grows the box
        by PaddleOCR's unclip distance, its area times unclip over its perimeter, so no letter loses an edge."""
        import av   # PyAV's swscale resizes the line, so no image library is needed
        np = self.np
        x0, y0, x1, y1 = box[:4]
        pad = max(2, int(round(LIMITS["unclip"] * (x1 - x0) * (y1 - y0) / (2 * (x1 - x0 + y1 - y0)))))
        crop = rgb[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        h, w = crop.shape[:2]
        nw = max(16, min(1600, int(round(48 * w / max(1, h)))))
        fr = av.VideoFrame.from_ndarray(np.ascontiguousarray(crop), format="rgb24").reformat(width=nw, height=48)
        x = (fr.to_ndarray().astype(np.float32)[:, :, ::-1] / 255.0 - 0.5) / 0.5   # BGR, as PaddleOCR trains
        y = self.rec[model].run(None, {"x": x.transpose(2, 0, 1)[None]})[0][0]
        out, probs, prev = [], [], 0
        for i, c in zip(y.argmax(1), y.max(1)):
            if i != prev and i != 0:
                out.append(self.chars[model][i])
                probs.append(float(c))
            prev = i
        return "".join(out), (float(np.mean(probs)) if probs else 0.0)


def line_boxes(p):
    """[(x0, y0, x1, y1, score)] of the text lines in probability map p: bands of rows, then runs of columns in each band."""
    import numpy as np
    b = p > LIMITS["det_bin"]
    rows = np.flatnonzero(b.any(1))
    out = []
    if not len(rows):
        return out
    for ys in np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1):
        y0, y1 = ys[0], ys[-1] + 1
        cols = np.flatnonzero(b[y0:y1].any(0))
        gap = max(4, int((y1 - y0) * 1.5))
        for xs in np.split(cols, np.flatnonzero(np.diff(cols) > gap) + 1):
            x0, x1 = xs[0], xs[-1] + 1
            rr = np.flatnonzero(b[y0:y1, x0:x1].any(1))
            yy0, yy1 = y0 + rr[0], y0 + rr[-1] + 1
            score = float(p[yy0:yy1, x0:x1][b[yy0:yy1, x0:x1]].mean())
            if score >= LIMITS["box_score"] and (x1 - x0) >= 3 and (yy1 - yy0) >= 3:
                out.append((int(x0), int(yy0), int(x1), int(yy1), round(score, 3)))
    return out


def same_box(a, b, W, H):
    tx, ty = LIMITS["overlay_tol"] * W, LIMITS["overlay_tol"] * H
    return abs(a[0] - b[0]) <= tx and abs(a[2] - b[2]) <= tx and abs(a[1] - b[1]) <= ty and abs(a[3] - b[3]) <= ty


def shaped(box, W, H, mid=False):
    """"bottom", "top" or None: where box sits when it has the shape of a subtitle line. With mid, "mid" for such a
    line between the bands."""
    x0, y0, x1, y1 = box[:4]
    cx, cy, w, h = (x0 + x1) / 2 / W, (y0 + y1) / 2 / H, (x1 - x0) / W, (y1 - y0) / H
    if (abs(cx - 0.5) > LIMITS["centre"] or not LIMITS["min_h"] <= h <= LIMITS["max_h"] or not LIMITS["min_w"] <= w <= LIMITS["max_w"]
            or (x1 - x0) < LIMITS["min_aspect"] * (y1 - y0)):
        return None
    return "bottom" if cy >= LIMITS["bottom"] else "top" if cy <= LIMITS["top"] else "mid" if mid else None


def analyse(frames, W, H):
    """frames: [{"kind", "boxes"}]. Sets each frame's "sub", {band: its subtitle lines}, empty for a frame with none. A
    box in the same place in overlay_share of all frames is an overlay and never a subtitle line. A crowded frame
    (credits, a title card) has none."""
    allb = [(i, bx) for i, f in enumerate(frames) for bx in f["boxes"] if shaped(bx, W, H)]
    n = max(1, len(frames))
    overlay = {(i, bx) for i, bx in allb if len({j for j, o in allb if same_box(bx, o, W, H)}) / n >= LIMITS["overlay_share"]}
    for i, f in enumerate(frames):
        lines = {"bottom": [], "top": []}
        for bx in f["boxes"]:
            where = shaped(bx, W, H)
            if where and (i, bx) not in overlay:
                lines[where].append(bx)
        centred = [shaped(bx, W, H, mid=True) for bx in f["boxes"]]
        crowded = centred.count("mid") >= LIMITS["crowd_mid"] or sum(1 for c in centred if c) >= LIMITS["crowd_all"]
        f["sub"] = {} if crowded else {k: v for k, v in lines.items() if 0 < len(v) <= LIMITS["max_lines"]}
    return frames


def verdict(frames, dur):
    """{"label": full, partial or none, "speech_share", "silence_share", "lift", "spread", "speech_frames",
    "silence_frames"} from frames [{"kind", "at", "sub"}]. A label needs a share of speech frames with a subtitle
    line, a lift of that share over the silence frames, and speech hits in enough tenths of the file. Credits and
    title sequences bunch up in time."""
    sp = [f for f in frames if f["kind"] == "speech"]
    si = [f for f in frames if f["kind"] == "silence"]
    s = sum(1 for f in sp if f["sub"]) / len(sp) if sp else 0.0
    q = sum(1 for f in si if f["sub"]) / len(si) if si else 0.0
    tenths = len({min(9, int(10 * f["at"] / max(dur, 1))) for f in sp if f["sub"]})
    label = ("full" if s >= LIMITS["full"] and s - q >= LIMITS["lift"] and tenths >= LIMITS["full_spread"] else
             "partial" if s >= LIMITS["partial"] and s - q >= LIMITS["lift"] and tenths >= LIMITS["partial_spread"] else "none")
    return {"label": label, "speech_share": round(s, 3), "silence_share": round(q, 3), "lift": round(s - q, 3), "spread": tenths,
            "speech_frames": len(sp), "silence_frames": len(si)}


# --- the language of the lines ---------------------------------------------------------------------------------------

def latin_language(texts):
    """"eng", "other" or None for Latin text. The share of each language is the share of the words of the text in its
    decide.STOPWORDS list. English must hold lang_cover, and lang_ratio times the next language. An English share
    under not_eng is another language, or look-alike letters of another script."""
    from . import decide
    joined = " ".join(texts).lower()
    if sum(ch.isalpha() for ch in joined) < LIMITS["lang_letters"]:
        return None
    words = decide.WORD.findall(joined)
    share = {k: sum(w in ws for w in words) / len(words) for k, ws in decide.STOPWORDS.items()}
    if share["eng"] >= LIMITS["lang_cover"] and share["eng"] >= LIMITS["lang_ratio"] * max(c for k, c in share.items() if k != "eng"):
        return "eng"
    return "other" if share["eng"] < LIMITS["not_eng"] else None


def script_of(text):
    """{script: letters} of text, for han, kana, hangul, latin and cyrillic."""
    c = {"han": 0, "kana": 0, "hangul": 0, "latin": 0, "cyrillic": 0}
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            c["han"] += 1
        elif 0x3040 <= o <= 0x30FF:
            c["kana"] += 1
        elif 0xAC00 <= o <= 0xD7AF:
            c["hangul"] += 1
        elif 0x0400 <= o <= 0x04FF:
            c["cyrillic"] += 1
        elif ch.isalpha() and o < 0x250:
            c["latin"] += 1
    return c


CJK = {"han": "chi", "kana": "jpn", "hangul": "kor"}


def language(ocr, frames):
    """{"english": True, False or None, "text_lang": 639-2 or None, "script": latin, cjk, other or None, "lines",
    "lines_read"} of the subtitle lines of up to rec_frames speech frames spread over the file. A line read at rec_conf
    or more counts. Latin text gets latin_language(). Chinese, Japanese and Korean letters name their language.
    Lines that mostly do not read are in a script the recognizers lack: not English, language unknown. Cyrillic that
    reads names no language, so it is None."""
    subbed = [f for f in frames if f["kind"] == "speech" and f["sub"]]
    k = LIMITS["rec_frames"]
    pick = [subbed[int(i * len(subbed) / k)] for i in range(k)] if len(subbed) > k else subbed
    texts, sc, lines, read = [], dict.fromkeys(script_of(""), 0), 0, 0
    for f in pick:
        for bxs in f["sub"].values():
            for bx in bxs:
                txt, conf = ocr.read(f["rgb"], bx)
                got = script_of(txt)
                if conf >= LIMITS["rec_conf"]:
                    for s, c in got.items():
                        sc[s] += c
                if got["latin"] > sum(got.values()) / 2:   # the Chinese recognizer joins some Latin words
                    txt, conf = ocr.read(f["rgb"], bx, "en")
                lines += 1
                if conf >= LIMITS["rec_conf"]:
                    texts.append(txt)
                    read += 1
    script = max(sc, key=sc.get) if sum(sc.values()) else None
    if script == "latin":
        lang = latin_language(texts)
    elif script in CJK:
        lang = CJK[script]
    elif lines >= 3 and read < LIMITS["unread"] * lines:
        lang = "other"
    else:
        lang = None
    return {"english": True if lang == "eng" else False if lang else None, "text_lang": None if lang == "other" else lang,
            "script": "latin" if script == "latin" else "cjk" if script in CJK else "other" if script or lang == "other" else None,
            "lines": lines, "lines_read": read}


# --- method B: strokes in the luma -----------------------------------------------------------------------------------

def strokes(y, H):
    """The pixels of short bright runs (a stroke) with a darker pixel close on both sides, row by row. A stroke is at
    most 0.02 of the height wide, and its sides lie within 0.008 of the height."""
    import numpy as np
    lmax, g = max(4, int(0.02 * H)), max(2, int(round(0.008 * H)))
    h, w = y.shape
    yp = np.pad(y, ((0, 0), (g, g + 1)), constant_values=255)
    b = np.zeros((h, w + 2 * g + 1), bool)
    b[:, g:g + w] = y >= METHOD_B["fill"]
    d = np.diff(b.astype(np.int8), axis=1)
    rs, cs = np.nonzero(d == 1)
    _, ce = np.nonzero(d == -1)
    cs += 1
    ce += 1                                  # a run is [cs, ce) in padded columns
    ln = ce - cs
    flat, W2 = yp.ravel(), yp.shape[1]
    ok = (ln >= 1) & (ln <= lmax)
    if not ok.any():
        return np.zeros_like(y, bool)
    rs, cs, ce = rs[ok], cs[ok], ce[ok]
    idx = np.empty(2 * len(cs), np.int64)
    idx[0::2], idx[1::2] = rs * W2 + cs, rs * W2 + ce
    fill = np.maximum.reduceat(flat, idx)[0::2]
    left = np.min(np.stack([yp[rs, cs - k] for k in range(1, g + 1)]), axis=0)
    right = np.min(np.stack([yp[rs, ce - 1 + k] for k in range(1, g + 1)]), axis=0)
    good = (left <= fill - METHOD_B["edge"]) & (right <= fill - METHOD_B["edge"])
    m = np.zeros_like(y, bool)
    for r, a, e in zip(rs[good], cs[good] - g, ce[good] - g):
        m[r, a:e] = True
    return m


def stroke_lines(mask, h0, H, Wd, min_starts):
    """Line boxes (y0, y1, x0, x1) as shares of the frame, in a band mask whose top row is row h0 of the frame. A
    line is a run of rows with min_starts stroke starts or more, with the shape and place of a subtitle line."""
    import numpy as np
    starts = np.count_nonzero(mask[:, 1:] & ~mask[:, :-1], axis=1)
    rows = starts >= min_starts
    out, y = [], 0
    while y < len(rows):
        if not rows[y]:
            y += 1
            continue
        z = y
        while z < len(rows) and (rows[z] or (z + 1 < len(rows) and rows[z + 1])):
            z += 1
        if METHOD_B["min_h"] <= (z - y) / H <= METHOD_B["max_h"]:
            cols = np.nonzero(mask[y:z].any(axis=0))[0]
            if len(cols):
                x0, x1 = np.percentile(cols, [2, 98])   # stray columns out
                if abs((x0 + x1) / 2 / Wd - 0.5) <= METHOD_B["centre"] and METHOD_B["min_w"] <= (x1 - x0) / Wd <= METHOD_B["max_w"]:
                    out.append((round((y + h0) / H, 3), round((z + h0) / H, 3), round(x0 / Wd, 3), round(x1 / Wd, 3)))
        y = z + 1
    return out


def method_b_lines(Y):
    """{"bot": [...], "top": [...]}: the stroke lines of luma frame Y in its bottom and top band."""
    import numpy as np
    H, Wd = Y.shape
    out = {}
    for name, (a, b) in (("bot", (int(METHOD_B["bottom"] * H), H - 2)), ("top", (2, int(METHOD_B["top"] * H)))):
        out[name] = stroke_lines(strokes(Y[a:b].astype(np.int16), H), a, H, Wd, max(8, Wd // 80))
    return out


def method_b_verdict(frames, dur):
    """verdict() of frames [{"kind", "at", "look": method_b_lines()}]. A line within overlay_tol in overlay_share of
    all frames is an overlay. A frame with any other line has a subtitle."""
    tol, share = LIMITS["overlay_tol"], LIMITS["overlay_share"]
    same = lambda b, o: all(abs(x - y) <= tol for x, y in zip(b, o))
    lines = [f["look"]["bot"] + f["look"]["top"] for f in frames]
    over = [b for ls in lines for b in ls if sum(1 for o in lines if any(same(b, x) for x in o)) >= share * len(frames)]
    return verdict([{"kind": f["kind"], "at": f["at"], "sub": {"x": 1} if any(not any(same(b, o) for o in over) for b in ls) else {}}
                    for f, ls in zip(frames, lines)], dur)


# --- the checks --------------------------------------------------------------------------------------------------------

def windows(dur, centres):
    """[[start, end]] of a window of lid.SAMPLE_SECS at each centre, a share of dur, in time order. Windows that
    overlap or touch merge, as in a short file."""
    out = []
    for c in sorted(centres):
        a = max(0.0, min(dur * c - lid.SAMPLE_SECS / 2, dur - lid.SAMPLE_SECS))
        b = min(dur, a + lid.SAMPLE_SECS)
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([round(a, 1), round(b, 1)])
    return out


def hear_window(path, audio_index, a, b, vad):
    """(spans, gaps) of speech in the audio of window [a, b], from Silero VAD with AMG's settings (subsync.voiced())."""
    import numpy as np
    from . import subsync
    pcm = lid.extract(path, audio_index, a, b - a)
    audio = np.frombuffer(pcm[:len(pcm) // 2 * 2], np.int16).astype(np.float32) / 32768
    end = round(a + len(audio) / 16000, 3)   # the audio the window really holds
    sp = [[round(a + x, 3), round(a + y, 3)] for x, y in subsync.voiced(vad(np.pad(audio, (0, -len(audio) % 512))).tolist())] if len(audio) else []
    sp = [[x, min(y, end)] for x, y in sp if x < end]
    return sp, gaps_of(sp, a, end)


def kind_in_windows(k, wins, spans):
    """"speech", "silence" or None for a keyframe at k seconds, by the spans of speech heard in windows wins. Speech
    holds k speech_pad inside a span. Silence holds k sil_pad from any speech and from the window's edges, because the
    audio past an edge is unknown. A keyframe outside the windows is None."""
    win = next(((a, b) for a, b in wins if a <= k <= b), None)
    if win is None:
        return None
    if any(a + LIMITS["speech_pad"] <= k <= b - LIMITS["speech_pad"] for a, b in spans):
        return "speech"
    if (any(a - LIMITS["sil_pad"] < k < b + LIMITS["sil_pad"] for a, b in spans) or k - win[0] < LIMITS["sil_pad"]
            or win[1] - k < LIMITS["sil_pad"]):
        return None
    return "silence"


def kind_in_file(k, spans, starts):
    """"speech", "silence" or None for a keyframe at k seconds, by the spans of speech of the whole file and their
    starts. The pads are those of kind_in_windows()."""
    i = bisect.bisect_right(starts, k) - 1
    if i >= 0 and spans[i][0] + LIMITS["speech_pad"] <= k <= spans[i][1] - LIMITS["speech_pad"]:
        return "speech"
    near = (i >= 0 and k - spans[i][1] < LIMITS["sil_pad"]) or (i + 1 < len(spans) and spans[i + 1][0] - k < LIMITS["sil_pad"])
    return None if near or (i >= 0 and k <= spans[i][1]) else "silence"


def sort(b, hdr=False):
    """quick()'s result from method B's verdict b: unsure under MIN_SPEECH_FRAMES speech keyframes, else flagged at a
    speech share of FLAG or more. B's label is not none only at a share of 0.08 or more, so the share alone decides.
    HDR video (hdr) under that share is unsure, because B misses its text, see HDR_TRC."""
    if b["speech_frames"] < MIN_SPEECH_FRAMES:
        return "unsure"
    return "flagged" if b["speech_share"] >= FLAG else "unsure" if hdr else "clean"


def quick(path, audio_index, duration):
    """The import's sort. {"result": "clean", "flagged" or "unsure", "hdr": True for video with a PQ or HLG transfer,
    which is never clean, "no_cues": True for a Matroska file without cues, which is unsure with no window heard,
    "windows": how many windows were heard, "speech_frames", "silence_frames", "b": method B's
    {"speech_share", "silence_share", "lift", "spread", "label"}, "took": wall seconds, "cpu": CPU seconds}. A flagged
    or unsure file needs full(). Raises when ffmpeg, the decode or the VAD model fails."""
    t0, c0 = time.time(), lid.cpu_now()
    from faster_whisper.vad import get_vad_model
    vad, heard, looked = get_vad_model(), {}, {}

    def look(sm, pk, k):   # a keyframe that two passes take decodes once
        if k not in looked:
            Y = sm.decode(pk, rgb=False)
            looked[k] = None if Y is None else method_b_lines(Y)
        return looked[k]

    centres = [(k + 0.5) / QUICK_WINDOWS for k in range(QUICK_WINDOWS)]
    sm = Sampler(path) if duration and duration > 0 else None
    hdr = bool(sm and sm.hdr)
    try:
        no_cues = bool(sm and sm.no_cues(duration))   # before an ffmpeg -ss reads the whole file to a window
        while sm and not no_cues:
            wins = windows(duration, centres)
            spans, gaps = [], []
            for a, b in wins:
                if (a, b) not in heard:
                    heard[a, b] = hear_window(path, audio_index, a, b, vad)
                spans += heard[a, b][0]
                gaps += heard[a, b][1]
            want = {"speech": quantile_points(spans, LIMITS["cand"] * N, LIMITS["speech_pad"]),
                    "silence": quantile_points(gaps, LIMITS["cand"] * N, LIMITS["sil_pad"])}
            frames = sample(sm, want, lambda k: kind_in_windows(k, wins, spans), look)
            b = method_b_verdict(frames, duration)
            if b["speech_frames"] >= MIN_SPEECH_FRAMES or len(centres) > QUICK_WINDOWS:
                break
            centres += [k / QUICK_WINDOWS for k in range(1, QUICK_WINDOWS)]   # the windows between: 19 evenly spaced in all
    finally:
        if sm:
            sm.close()
    if not sm or no_cues:
        wins, b = [], verdict([], 0)
    return {"result": sort(b, hdr), "hdr": hdr, "no_cues": no_cues, "windows": len(wins), "speech_frames": b.pop("speech_frames"), "silence_frames": b.pop("silence_frames"),
            "b": b, "took": round(time.time() - t0, 2), "cpu": round(lid.cpu_now() - c0, 2)}


def full(path, audio_index, duration, second=False, gate=None, queue=None, cache=lid.CACHE, model_dir=lid.MODEL_DIR):
    """The background check that decides. {"label": "full", "partial", "none" or "unsure" (under MIN_SPEECH_FRAMES
    speech frames), "english": True, False or None (the
    reader cannot tell), "text_lang", "script", see language(), "speech_share", "silence_share", "lift", "spread",
    "speech_frames", "silence_frames", "lines", "lines_read", "second", "spans_cached", "took": wall seconds, "cpu": CPU
    seconds}. second samples frames that are no keyframe, so none of them is a frame of the first pass. Many files
    hold too few keyframes in speech for a second set of keyframes. lid.speech() yields
    its speech read at gate and queue, see lid.waits(). The sampling asks the same after every 10 frames. A yield
    answers {"yielded": True, "took"}. Raises when a decode or a model fails."""
    t0, c0 = time.time(), lid.cpu_now()
    sm = Sampler(path)   # a file with no video fails before the speech read
    try:
        sp = lid.speech(path, audio_index, cache, gate, queue)
        if sp.get("yielded"):
            return {"yielded": True, "took": round(time.time() - t0, 2)}
        spans, starts = sp["spans"], [x[0] for x in sp["spans"]]
        want = {"speech": quantile_points(spans, LIMITS["cand"] * N, LIMITS["speech_pad"]),
                "silence": quantile_points(gaps_of(spans, 0.0, duration), LIMITS["cand"] * N, LIMITS["sil_pad"])}
        ocr, (W, H) = Ocr(model_dir), sm.size
        keep = lambda bx: any(shaped(b, W, H) for b in bx)   # a frame the recognizers may read keeps its pixels

        def look(sm, pk, k):
            rgb = sm.decode(pk, rgb=True)
            if rgb is None:
                return None
            bx = ocr.boxes(rgb)
            return {"boxes": bx, "rgb": rgb if keep(bx) else None}
        frames = sample(sm, want, lambda k: kind_in_file(k, spans, starts), look, exact=second, stop=lambda: lid.waits(gate, queue))
    finally:
        sm.close()
    if frames is None:
        return {"yielded": True, "took": round(time.time() - t0, 2)}
    if not frames and (want["speech"] or want["silence"]):
        raise RuntimeError("no keyframe decoded")
    frames = analyse([dict(f, **f.pop("look")) for f in frames], W, H)
    v = verdict(frames, duration)
    if v["speech_frames"] < MIN_SPEECH_FRAMES:
        v["label"] = "unsure"
    return dict(v, **language(ocr, frames), second=second, spans_cached=bool(sp.get("cached")),
                took=round(time.time() - t0, 2), cpu=round(lid.cpu_now() - c0, 2))


# --- install ---------------------------------------------------------------------------------------------------------

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ready(model_dir=lid.MODEL_DIR):
    """(True, "") when every model is in model_dir and matches its sha256, else (False, the first reason). Stdlib only,
    for the start checks and --selftest."""
    for name, (src, want) in MODELS.items():
        path = os.path.join(model_dir, MODEL_SUB, os.path.basename(src))
        try:
            got = sha256(path)
        except OSError as ex:
            return False, f"the {name} model does not read: {ex.strerror or ex}"
        if got != want:
            return False, f"{path} has sha256 {got}, the pin is {want}"
    return True, ""


def fetch(model_dir=lid.MODEL_DIR):
    """Install time only. Download each pinned model that is missing or wrong into model_dir/ocr and check its sha256.
    A download goes to a temporary name first, so a killed run leaves no half file under the real name. Returns
    "present" or "downloaded". Raises on a wrong sha256, so a bad model never reaches the check."""
    dest, done = os.path.join(model_dir, MODEL_SUB), "present"
    os.makedirs(dest, exist_ok=True)
    for src, want in MODELS.values():
        path = os.path.join(dest, os.path.basename(src))
        if os.path.exists(path) and sha256(path) == want:
            continue
        part = path + ".part"
        with urllib.request.urlopen(MODEL_URL + src, timeout=120) as r, open(part, "wb") as f:
            for block in iter(lambda: r.read(1 << 20), b""):
                f.write(block)
        got = sha256(part)
        if got != want:
            os.remove(part)
            raise RuntimeError(f"{MODEL_URL + src} has sha256 {got}, the pin is {want}")
        os.replace(part, path)
        done = "downloaded"
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check one file for burned-in subtitles. Prints one JSON line.")
    ap.add_argument("path", nargs="?")
    ap.add_argument("index", type=int, nargs="?", help="the ffmpeg audio index, 0:a:INDEX")
    ap.add_argument("duration", type=float, nargs="?")
    ap.add_argument("--fetch", action="store_true", help="install time: download the pinned models and check each sha256")
    ap.add_argument("--quick", action="store_true", help="the import's sort, see quick()")
    ap.add_argument("--full", action="store_true", help="the check that decides, see full()")
    ap.add_argument("--second", action="store_true", help="with --full, take keyframes the first pass did not take")
    ap.add_argument("--cache", default=lid.CACHE, help="lid.py's cache, where the spans of speech are")
    ap.add_argument("--model-dir", default=lid.MODEL_DIR)
    ap.add_argument("--yield-gate", help="yield while another hearing holds this gate file, see lid.waits()")
    ap.add_argument("--yield-queue", help="yield while an import job waits in this state store, see lid.waits()")
    a = ap.parse_args(argv)
    if a.fetch:
        print(fetch(a.model_dir))
        return 0
    if a.duration is None or a.quick == a.full:
        ap.error("PATH, INDEX, DURATION and one of --quick and --full are required")
    lid.lower_priority(THREADS)
    try:
        if a.quick:
            r = quick(a.path, a.index, a.duration)
        else:
            r = full(a.path, a.index, a.duration, a.second, a.yield_gate, a.yield_queue, a.cache, a.model_dir)
    except Exception as e:   # the hook reads one JSON line either way
        r = {"error": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(r))
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.exit(main())
