# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The guard of the time reads (docs/design.md, "Time reads"). A check or a write takes each time from the file as it
stores it, or as its spec defines it: matroska.frames(), mp4.samples() or ts.frames(). ff.py is the one door to
ffmpeg, ffprobe and PyAV. Each call names its use, and ff.READS names the times that use may read, with the reason.
ff.py allows only what it knows, so a time the use does not name, and anything ff.py does not know, raises TimeRead at
run time. Its audit hook refuses a start of ffmpeg or ffprobe, or an import of PyAV, from any other module of the
package. The scan here reads the source, so it covers the paths that no test runs."""
import ast, concurrent.futures, json, multiprocessing, os, pathlib, re, shutil, subprocess, sys, textwrap, time

import pytest

import amg

hook = amg.load()
ff = sys.modules["arr_media_guard.ff"]
ROOT = pathlib.Path(amg.PACKAGE)

# WORDS name a time in ffmpeg or ffprobe output, each with a pattern of a string that holds it and the test of a name
# of ff.READS that covers it. They cover the packet, frame, format and stream fields, the framemd5, framecrc and
# framehash muxers, the packet and frame dumps, showinfo, -progress, -debug_ts, the Duration of the input dump, the
# time= of the stats line, the DURATION tag, the decoded sample count and the display times of a subtitle. ATTRS name
# the times PyAV gives a packet, a frame, a stream or a container. A read of such an attribute counts, and a call of a
# method of that name does not (decide.duration(j)). time.time() is the wall clock, so it never counts. An argv that
# starts ffmpeg or ffprobe counts too, by the list element "ffmpeg" or "ffprobe".
end = lambda *fields: lambda n: any(n.endswith(f".{f}") for f in fields)
WORDS = {"pts_time": (r"pts_time", end("pts_time")), "dts_time": (r"dts_time", end("dts_time")),
         "duration_time": (r"duration_time", end("duration_time")),
         "best_effort_timestamp": (r"best_effort_timestamp", end("best_effort_timestamp", "best_effort_timestamp_time")),
         **{m: (m, lambda n, m=m: n == m) for m in ("framemd5", "framecrc", "framehash")},
         "-show_packets": (r"-show_packets", lambda n: n.startswith("packet.")), "packet=": (r"packet=", lambda n: n.startswith("packet.")),
         "-show_frames": (r"-show_frames", lambda n: n.startswith("frame.")), "frame=": (r"frame=", lambda n: n.startswith("frame.")),
         "=duration": (r"=duration", end("duration")), "start_time": (r"start_time", end("start_time")),
         "out_time": (r"out_time", lambda n: n.startswith("progress.out_time")), "showinfo": (r"showinfo", lambda n: n == "showinfo"),
         "-debug_ts": (r"-debug_ts", lambda n: n == "debug_ts"), "Duration": (r"\bDuration: ", lambda n: False),
         "time=": (r"(?<!\w)time=", lambda n: n == "stats"), "DURATION": (r"^DURATION$|DURATION(?:\s*:|\*|\\s)", lambda n: n == "setts"),
         "nb_samples": (r"\bnb_samples\b", end("nb_samples")), "start_display_time": (r"\bstart_display_time\b", end("start_display_time")),
         "end_display_time": (r"\bend_display_time\b", end("end_display_time"))}
ATTRS = ("pts", "dts", "best_effort_timestamp", "time", "duration", "start_time", "timestamp", "samples")
TOOLS = ("ffmpeg", "ffprobe")
CALLS = ("run", "popen", "pyav", "lines", "Log")   # the functions of ff that take a use first


def units(path):
    """{unit: (hits, the uses its ff calls name, the strings and attributes of its code)} of the module at path. A hit
    is (a key of WORDS, ".attribute" of ATTRS or a tool, its line). A unit is a top-level function or class with
    everything inside it, as "module.name", or "module.<module>" for the code outside them. A docstring and a comment
    are no code. An ff call is ff.run(), ff.popen(), ff.pyav(), ff.lines() or ff.Log() with a use first, or
    run_bounded() with use=. A called attribute shows in the strings as ".name()"."""
    out, stem = {}, path.stem

    def code(body):   # the statements of a body without its docstring
        doc = body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)
        return body[1:] if doc else body

    def walk(node, unit, called):
        u = out.setdefault(unit, ([], set(), set()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for x in (*node.decorator_list, *([node.args] if hasattr(node, "args") else []), *code(node.body)):
                walk(x, unit, False)
            return
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
            if owner == "ff" and name in CALLS and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value is not None:
                u[1].add(node.args[0].value)   # use None starts another tool, as mkvmerge, see ff.run()
            if name == "run_bounded":
                u[1].update(k.value.value for k in node.keywords if k.arg == "use" and isinstance(k.value, ast.Constant))
            walk(node.func, unit, True)
            for x in (*node.args, *node.keywords):
                walk(x, unit, False)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            u[2].add(node.value)
            u[0].extend((w, node.lineno) for w, (pat, _) in WORDS.items() if re.search(pat, node.value))
        if isinstance(node, (ast.List, ast.Tuple)):
            u[0].extend((x.value, node.lineno) for x in node.elts if isinstance(x, ast.Constant) and x.value in TOOLS)
        wall = isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (node.value.id, node.attr) == ("time", "time")
        if isinstance(node, ast.Attribute) and not wall:
            u[2].add(f".{node.attr}()" if called else f".{node.attr}")
            if node.attr in ATTRS and not called:
                u[0].append((f".{node.attr}", node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, unit, False)

    for node in code(ast.parse(path.read_text()).body):
        name = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else "<module>"
        walk(node, f"{stem}.{name}", False)
    return out


def scan():
    """units() of every module of the package but ff.py."""
    return {k: v for p in sorted(ROOT.glob("*.py")) if p.name != "ff.py" for k, v in units(p).items()}


def readers():
    """[(unit, use)] of each reader that ff.READS names."""
    return [(r, use) for use, u in ff.READS.items() for r in u.readers]


def covered(hit, names, uses):
    """Whether a name of names covers hit, see WORDS. A tool list needs an ff call of the unit."""
    if hit in TOOLS:
        return bool(uses)
    if hit.startswith("."):
        return any(n.endswith(hit) for n in names)
    return any(WORDS[hit][1](n) for n in names)


def bare(found, reads):
    """{unit: [hit@line]} of each time word of a unit of found that no name of the uses it calls or reads for covers."""
    by = {}
    for r, use in readers():
        by.setdefault(r, set()).add(use)
    bad = {}
    for unit, (hits, uses, _) in found.items():
        mine = (uses | by.get(unit, set())) & set(reads)
        names = [n for u in mine for n in reads[u].names]
        for hit, line in hits:
            if not covered(hit, names, mine):
                bad.setdefault(unit, []).append(f"{hit}@{line}")
    return bad


def test_every_time_word_of_a_unit_is_a_name_of_a_use_it_calls_or_reads_for():
    found = scan()
    assert not (bad := bare(found, ff.READS)), f"these units hold a time word that no use they call, or read for, names in ff.READS: {bad}"
    unknown = {k: sorted(uses - set(ff.READS)) for k, (_, uses, _) in found.items() if uses - set(ff.READS)}
    assert not unknown, f"these ff calls name a use that ff.READS lacks: {unknown}"


def test_the_scan_takes_no_use_whose_names_miss_the_word(tmp_path):
    """A unit that calls a use with no time names, as checks.ffprobe_audio, and holds a time word fails. The new words
    of the dump, the stats line, the DURATION tag and the sample count count too."""
    path = tmp_path / "m.py"
    path.write_text('def a(p):\n    r = ff.run("checks.ffprobe_audio", [])\n    return r["start_time"]\n\n\n'
                    'def b(p):\n    r = ff.run("content.last_packet", [])\n    return r["pts_time"]\n\n\n'
                    'def c(e):\n    ff.run("checks.sample", [])\n    return re.findall(r"Duration: (\\S+)", e), "time=", e["DURATION"], e["nb_samples"]\n')
    assert bare(units(path), ff.READS) == {"m.a": ["start_time@3"], "m.c": ["Duration@13", "time=@13", "DURATION@13", "nb_samples@13"]}


def said(word, texts):
    """Whether a string or an attribute of texts, see units(), holds word as a whole word. seek shows as -ss,
    -read_intervals or a call of .seek(), the dump as the lines a parser looks for, aresample as a call of ff.clock(),
    and a filter in its log name too."""
    alts = {"seek": ("-ss", "-read_intervals", ".seek()"), "dump": ("Output #", "encv"), "aresample": ("aresample", ".clock()")}.get(
        word, (word, f"Parsed_{word}_0"))
    return any(t in (f".{w}", f".{w}()") or re.search(rf"(?:(?<![A-Za-z_])|(?<=\\b)){re.escape(w)}(?![A-Za-z_])", t) for t in texts for w in alts)


def test_every_entry_of_ff_reads_is_in_use():
    found = scan()
    called = {u for _, uses, _ in found.values() for u in uses}
    assert not set(ff.READS) - called, f"no ff call names these uses, take them out of ff.READS: {sorted(set(ff.READS) - called)}"
    words = lambda use: [n.partition(".")[2] or n for n in ff.READS[use].names]
    gone = sorted(r for r, use in readers() if r not in found or not (found[r][0] or any(said(w, found[r][2]) for w in words(use))))
    assert not gone, f"these readers of ff.READS read no time of their use any more, take them out: {gone}"


def test_every_name_of_ff_reads_is_read_where_its_use_runs():
    """A name a use does not read is a widened entry. Each name of an entry must show in the code of a unit that calls
    the use or reads its output: as a word of a string (pts_time, out_time_us, -progress), or as an attribute (.pts).
    The time columns of framemd5 are read by their place in the line, so they show nowhere."""
    found, wide = scan(), {}
    for use, u in ff.READS.items():
        texts = set().union(*(v[2] for k, v in found.items() if use in v[1] or k in u.readers))
        for name in u.names:
            channel, _, word = name.partition(".")
            if channel in ("framemd5", "framecrc", "framehash") and word:
                continue
            if not said(word or name, texts):
                wide.setdefault(use, []).append(name)
    assert not wide, f"ff.READS names times that no code of the use reads: {wide}"


def test_no_module_but_ff_imports_pyav():
    """PyAV comes through ff.pyav(). No module of the package imports ctypes, _posixsubprocess or multiprocessing, or
    names ProcessPoolExecutor, because each one starts a process or calls C that the audit hook sees in part only."""
    def banned(p, module):
        top = module.split(".")[0]
        return top in ("ctypes", "_posixsubprocess", "multiprocessing") or top == "av" and p.name != "ff.py"

    bad = [f"{p.name}:{n.lineno}" for p in sorted(ROOT.glob("*.py")) for n in ast.walk(ast.parse(p.read_text()))
           if isinstance(n, ast.Import) and any(banned(p, a.name) for a in n.names)
           or isinstance(n, ast.ImportFrom) and not n.level and banned(p, n.module or "")
           or isinstance(n, ast.ImportFrom) and any(a.name == "ProcessPoolExecutor" for a in n.names)
           or "ProcessPoolExecutor" in (getattr(n, "id", None), getattr(n, "attr", None))]
    assert not bad, f"PyAV comes through ff.pyav(), and no module starts a process in these ways: {bad}"


def test_no_module_but_ff_reads_the_object_behind_a_proxy():
    """_o, _m, _it and _seen of a proxy of ff.py, and ff._unwrap(), give the PyAV object, whose times no allow list
    guards (N12). No module of the package but ff.py reads them, by attribute, by name or by a string."""
    private = ("_o", "_m", "_it", "_seen", "_unwrap")
    bad = [f"{p.name}:{n.lineno}" for p in sorted(ROOT.glob("*.py")) if p.name != "ff.py" for n in ast.walk(ast.parse(p.read_text()))
           if getattr(n, "attr", None) in private or getattr(n, "id", None) in private or isinstance(n, ast.Constant) and n.value in private
           or isinstance(n, ast.alias) and n.name in private]
    assert not bad, f"only ff.py reads the PyAV object behind a proxy: {bad}"


def test_no_function_that_calls_ff_binds_a_name_ff():
    """A local ff hides the module in the whole function, and its call then raises UnboundLocalError."""
    bad = []
    for p in sorted(ROOT.glob("*.py")):
        for fn in ast.walk(ast.parse(p.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound = any(isinstance(n, ast.Name) and n.id == "ff" and isinstance(n.ctx, ast.Store) for n in ast.walk(fn))
                calls = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ff" and hasattr(ff, n.attr)
                            for n in ast.walk(fn))
                if bound and calls:
                    bad.append(f"{p.name}:{fn.lineno} {fn.name}")
    assert not bad, bad


def test_the_scan_finds_a_read_in_code_and_never_in_a_docstring(tmp_path):
    path = tmp_path / "m.py"
    path.write_text('def a():\n    """It reads pts_time."""\n    return time.time() + decide.duration(j)\n\n\ndef b(p):\n'
                    '    x = ["ffprobe", "-show_entries", "packet=pts_time"]\n    return p.pts\n\n\nclass C:\n    def d(self):\n'
                    '        return ff.run("checks.window", ["ffmpeg", "-vf", "showinfo"])\n')
    got = {k: (sorted(v[0]), v[1]) for k, v in units(path).items() if v[0] or v[1]}
    assert got == {"m.b": ([(".pts", 8), ("ffprobe", 7), ("packet=", 7), ("pts_time", 7)], set()),
                   "m.C": ([("ffmpeg", 13), ("showinfo", 13)], {"checks.window"})}


# The shapes of a time read that the source scan of 2.18.6 missed (amg-54 review), and the escapes of review A of
# amg-55. Each one fails at run time.
@pytest.fixture
def clip(tmp_path_factory):
    """A made-up Matroska file: 2 s of video and audio, a SubRip track and two chapters."""
    path = tmp_path_factory.getbasetemp() / "time-reads.mkv"
    if not path.exists():
        srt, meta = path.with_suffix(".srt"), path.with_suffix(".ffmeta")
        srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello.\n")
        meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=a\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=b\n")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=10:duration=2", "-f", "lavfi", "-i",
                        "sine=duration=2", "-i", str(srt), "-i", str(meta), "-map", "0", "-map", "1", "-map", "2", "-map_chapters", "3",
                        "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "srt", str(path)], check=True)
    return str(path)


@pytest.fixture
def use(monkeypatch, tmp_path):
    """use(*names) adds a made-up use to ff.READS and returns its name. The refusals of the test go to a file of its own."""
    monkeypatch.setenv("AMG_FF_FAULTS", str(tmp_path / "faults.txt"))

    def add(*names):
        monkeypatch.setitem(ff.READS, "test.shape", ff.Use(names, "a test"))
        return "test.shape"
    return add


def test_all_packet_fields_then_a_key(clip, use):
    r = ff.run(use(), ["ffprobe", "-v", "error", "-show_packets", "-of", "json", clip], capture_output=True, text=True)
    p = ff.loads(r)["packets"][0]
    assert p["size"] and "pts_time" not in r.stdout
    with pytest.raises(ff.TimeRead, match="pts_time"):
        p["pts_time"]


def test_a_dict_key_of_a_probe(clip, use):
    r = ff.run(use("stream.duration"), ["ffprobe", "-v", "error", "-show_streams", "-of", "json", clip], capture_output=True, text=True)
    s = ff.loads(r)["streams"][0]
    assert s["codec_type"] == "video" and "start_time" not in r.stdout and s.get("duration") is None   # a Matroska stream has none
    with pytest.raises(ff.TimeRead, match="start_time"):
        s["start_time"]


def test_a_get_of_a_probe(clip, use):
    d = ff.loads(ff.run(use(), ["ffprobe", "-v", "error", "-show_entries", "stream=index:format=format_name", "-of", "json", clip],
                        capture_output=True, text=True))
    for x in (d["streams"][0], d["format"]):   # the field is not even there, and the read still fails
        with pytest.raises(ff.TimeRead):
            x.get("start_time")
        with pytest.raises(ff.TimeRead):
            "duration" in x


def test_a_field_after_a_comma(clip, use):
    with pytest.raises(ff.TimeRead, match="packet.pts"):
        ff.run(use(), ["ffprobe", "-v", "error", "-show_entries", "packet=" + ",".join(("size", "pts")), "-of", "csv", clip])
    with pytest.raises(ff.TimeRead, match="names packet.dts"):   # a name the narrowed read does not ask for is a widened entry
        ff.run(use("packet.pts", "packet.dts"), ["ffprobe", "-v", "error", "-show_entries", "packet=size,pts", "-of", "csv", clip])


def test_a_helper_that_reads(clip, use):
    def entries(*fields):   # the argv comes from elsewhere
        return ["-show_entries", "format=" + ",".join(fields)]

    with pytest.raises(ff.TimeRead, match="format.duration"):
        ff.run(use(), ["ffprobe", "-v", "error", *entries("format_name", "duration"), clip])
    first = lambda d: d["streams"][0].get("duration")   # a helper of the probe
    with pytest.raises(ff.TimeRead):
        first(ff.loads(ff.run(use(), ["ffprobe", "-v", "error", "-show_streams", "-of", "json", clip], capture_output=True, text=True)))


def test_showinfo_text_parsed_apart_from_its_filter(clip, use):
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-i", clip, "-map", "0:v", "-vf", "showinfo",
            "-f", "null", "-"]
    with pytest.raises(ff.TimeRead, match="showinfo"):
        ff.run(use(), argv)
    err = ff.run(use("showinfo", "showinfo.pts_time"), argv, capture_output=True, text=True).stderr
    parse = lambda text, k: ff.re.findall(rf"\b{k}:\s*(\S+)", text)   # a parser in another module
    assert len(parse(err, "pts_time")) == 20 and parse(err, "pts") == parse(err, "duration_time") == parse(err, "duration") == []
    with pytest.raises(ff.TimeRead, match="progress"):
        ff.run(use(), ["ffmpeg", "-nostdin", "-v", "error", "-progress", "pipe:1", "-i", clip, "-f", "null", "-"])
    with pytest.raises(ff.TimeRead, match="level"):   # ffmpeg's info lines with no level on each
        ff.run(use(), ["ffmpeg", "-nostdin", "-i", clip, "-f", "null", "-"])
    with pytest.raises(ff.TimeRead, match="debug_ts"):
        ff.run(use(), ["ffmpeg", "-nostdin", "-v", "error", "-debug_ts", "-i", clip, "-f", "null", "-"])
    for mux in ("framemd5", "framecrc", "framehash"):
        with pytest.raises(ff.TimeRead, match=mux):
            ff.run(use(), ["ffmpeg", "-nostdin", "-v", "error", "-i", clip, "-c", "copy", "-f", mux, "-"])


def test_the_decoded_sample_count_is_named_as_a_count(clip, use):
    """volumedetect's n_samples counts the decoded samples. It says how much audio decodes and places no frame. A use
    names it, as checks.sample() and checks.full_decode() do, or it goes."""
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-i", clip, "-map", "0:a", "-af", "volumedetect",
            "-f", "null", "-"]
    assert "n_samples" not in ff.run(use("volumedetect"), argv, capture_output=True, text=True).stderr
    assert hook.arr_decide.parse_sample(ff.run(use("volumedetect", "volumedetect.n_samples"), argv, capture_output=True, text=True).stderr)["n"] > 0
    assert set(ff.READS["checks.sample"].names) == {"volumedetect", "volumedetect.n_samples", "dump", "seek"}


def test_whisper_hears_raw_pcm_with_no_time_field(clip, use):
    """The word times of Whisper count the samples of raw PCM from the -ss start. lid.extract() names no time field,
    only the seek that ffmpeg's frame times place and aresample, which puts the samples on the file's clock. A muxer or
    a filter that gives a time fails."""
    argv = ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", "0.5", "-i", clip, "-t", "1", "-map", "0:a", "-af", ff.clock(), "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-"]
    assert ff.READS["lid.extract"].names == ("seek", "aresample") and ff.check("lid.extract", argv)["got"] == {"seek", "aresample"}
    assert len(ff.run("lid.extract", argv, capture_output=True).stdout) > 30000
    with pytest.raises(ff.TimeRead):
        ff.check("lid.extract", argv[:-3] + ["-af", "ashowinfo", "-f", "s16le", "-"])
    with pytest.raises(ff.TimeRead, match="seek"):   # a cut by ffmpeg's frame times needs "seek"
        ff.check(use(), argv)
    probe = ["ffprobe", "-v", "error", "-show_entries", "packet=size", "-of", "csv=p=0", clip]
    for interval in ("%20", "%+20", "10%+#5", "5", "%+#5,%3"):   # N7: an end time bounds the read by time too
        with pytest.raises(ff.TimeRead, match="seek"):
            ff.check(use(), probe[:-1] + ["-read_intervals", interval, clip])
    assert ff.check(use(), probe[:-1] + ["-read_intervals", "%+#5", clip])["got"] == set()


def test_progress_keys_and_framemd5_columns_the_use_does_not_name_go(clip, use, tmp_path):
    out = ff.run(use("progress", "progress.out_time_us"), ["ffmpeg", "-nostdin", "-v", "error", "-progress", "pipe:1", "-i", clip, "-f", "null", "-"],
                 capture_output=True, text=True).stdout
    assert "out_time_us=" in out and "out_time_ms=" not in out and "out_time=" not in out and "speed=" not in out
    md5 = tmp_path / "x.md5"
    for extra in ([], ["-y"]):   # an option between -f and the path
        ff.run(use("framemd5"), ["ffmpeg", "-nostdin", "-v", "error", "-i", clip, "-map", "0:v", "-c", "copy", "-f", "framemd5", *extra, str(md5)])
        rows = [line.split(",") for line in md5.read_text().splitlines() if line[:1].isdigit()]
        assert len(rows) == 20 and all(r[1:4] == ["", "", ""] and len(r[5].strip()) == 32 for r in rows)
    ff.run(use("framemd5"), ["ffmpeg", "-nostdin", "-v", "error", "-i", clip, "-map", "0:v", "-c", "copy", "-f", "framemd5", "rel.md5"],
           cwd=tmp_path)   # N2: ffmpeg writes a relative path under cwd
    assert [line.split(",")[1:4] for line in (tmp_path / "rel.md5").read_text().splitlines() if line[:1].isdigit()] == [["", "", ""]] * 20
    with pytest.raises(ff.TimeRead, match="ff.run"):
        ff.popen(use("framemd5"), ["ffmpeg", "-nostdin", "-v", "error", "-i", clip, "-f", "framemd5", "-"])


def test_a_call_with_no_entry_fails(clip, use):
    with pytest.raises(ff.TimeRead, match="no entry"):
        ff.run("test.none", ["ffprobe", "-v", "error", clip])
    with pytest.raises(ff.TimeRead, match="no entry"):
        hook.run_bounded(["ffprobe", "-v", "error", clip], 10)
    assert hook.run_bounded(["mkvmerge", "--version"], 10, capture_output=True).returncode == 0   # another tool needs no use


def test_a_launcher_before_ffprobe_a_shell_and_executable_fail(clip, use):
    """A1: only ionice and nice may come before ffmpeg or ffprobe. ff.run() takes an argv list, with no shell and no
    executable=. The audit hook refuses the launcher, as ff.run() is package code."""
    probe = ["ffprobe", "-v", "error", "-show_entries", "packet=pts_time", "-of", "csv", clip]
    for argv in (["timeout", "60", *probe], ["stdbuf", "-oL", *probe], ["env", "X=1", *probe], ["sh", "-c", " ".join(probe)]):
        with pytest.raises(ff.TimeRead, match=rf"ff.py:\d+ starts {argv[0]}, which ff.py does not allow"):
            ff.run(use("packet.pts_time"), argv, capture_output=True, text=True)
    with pytest.raises(ff.TimeRead, match="argv list"):
        ff.run(use(), " ".join(probe), shell=True)
    with pytest.raises(ff.TimeRead, match="argv list"):
        ff.run(use(), ["x", *probe[1:]], executable=shutil.which("ffprobe"))
    assert ff.check(use(), ["ionice", "-c3", "nice", "-n", "19", "ffprobe", "-v", "error", clip])["tool"] == "ffprobe"


def test_an_output_ff_cannot_filter_fails(clip, use, tmp_path):
    """A2: an output that needs a filter comes back as captured text. Bytes, a file, an inherited pipe, ffprobe -o and
    a path ff.py does not own fail. A2+: popen() filters its pipes, so a named text channel on a pipe fails."""
    whole = ["ffprobe", "-v", "error", "-show_packets", "-of", "json", clip]
    for kw in ({"capture_output": True}, {"stdout": open(tmp_path / "o.json", "w"), "text": True}, {}):
        with pytest.raises(ff.TimeRead, match="bytes, in a file"):
            ff.run(use(), whole, **kw)
    with pytest.raises(ff.TimeRead, match="-o"):
        ff.run(use(), ["ffprobe", "-v", "error", "-o", str(tmp_path / "o.json"), *whole[3:]], capture_output=True, text=True)
    for path in ("file:" + str(tmp_path / "x.md5"), "/dev/stdout", "pipe:2"):
        with pytest.raises(ff.TimeRead, match="does not allow"):
            ff.run(use("framemd5"), ["ffmpeg", "-nostdin", "-v", "error", "-i", clip, "-f", "framemd5", path], capture_output=True, text=True)
    with pytest.raises(ff.TimeRead, match="bytes, in a file"):   # framemd5 bytes on the pipe
        ff.run(use("framemd5"), ["ffmpeg", "-nostdin", "-v", "error", "-i", clip, "-f", "framemd5", "-"], capture_output=True)
    sample = ["ffmpeg", "-nostdin", "-nostats", "-loglevel", "level+info", "-i", clip, "-map", "0:a", "-af", "volumedetect", "-f", "null", "-"]
    with pytest.raises(ff.TimeRead, match="ff.Log"):
        ff.popen(use("volumedetect"), sample, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    with ff.Log(use("volumedetect"), str(tmp_path)) as log:
        ff.popen(use("volumedetect"), sample, stdout=subprocess.DEVNULL, stderr=log).wait()
        text = log.read()
    assert "max_volume" in text and "n_samples" not in text and "Duration" not in text and "time=" not in text


def test_an_ffprobe_section_or_field_ff_does_not_know_fails(clip, use):
    """A3: a section outside the allow list fails, and JSON keeps only known keys, nested ones too: the DURATION tag of
    a stream goes. A3+: packets_and_frames keeps each item by its type."""
    for entries in ("subtitle=pts_time", "program_stream=start_time", "frame=nb_samples", "chapter=start", "stream=nb_frames"):
        with pytest.raises(ff.TimeRead):
            ff.run(use(), ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", clip], capture_output=True, text=True)
    d = ff.loads(ff.run(use(), ["ffprobe", "-v", "error", "-show_entries", "stream_tags", "-of", "json", clip], capture_output=True, text=True))
    tags = d["streams"][0]["tags"]
    assert "DURATION" not in json.dumps(d) and dict(tags) == {}
    with pytest.raises(ff.TimeRead, match="DURATION"):
        tags.get("DURATION")
    with pytest.raises(ff.TimeRead, match="frame"):
        ff.run(use(), ["ffprobe", "-v", "error", "-show_packets", "-show_frames", "-of", "json", clip], capture_output=True, text=True)
    both = ff.strip(use(), {"packets_and_frames": [{"type": "packet", "size": "9", "pts_time": "0.5"}, {"type": "frame", "pts_time": "0.5"}]})
    p, f = both["packets_and_frames"]
    assert dict(p) == {"size": "9"} and dict(f) == {}
    with pytest.raises(ff.TimeRead):
        p["pts_time"]
    with pytest.raises(ff.TimeRead):
        both.get("programs")


def test_a_filter_graph_outside_the_filter_options_fails(clip, use, tmp_path):
    """A4: a lavfi input and a filter script are filter graphs. A filter ff.py does not know fails, as vfrdet (A4+)."""
    script = tmp_path / "g.txt"
    script.write_text("showinfo")
    for argv in (["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"movie={clip},showinfo", "-f", "null", "-"],
                 ["ffprobe", "-v", "error", "-f", "lavfi", "-i", f"movie={clip},showinfo"],
                 ["ffmpeg", "-v", "error", "-i", clip, "-filter_script:v", str(script), "-f", "null", "-"],
                 ["ffmpeg", "-v", "error", "-i", clip, "-/vf", str(script), "-f", "null", "-"],
                 ["ffmpeg", "-v", "error", "-i", clip, "-filter_complex_script", str(script), "-f", "null", "-"],
                 ["ffmpeg", "-v", "error", "-i", clip, "-vf", "vfrdet", "-f", "null", "-"]):
        with pytest.raises(ff.TimeRead, match="does not know"):
            ff.check(use(), argv)


def test_a_filter_takes_only_the_arguments_ff_knows(clip, use, monkeypatch):
    """N1: an expression filter evaluates its argument for each frame, and print() in it logs a time at level warning.
    A filter or a bitstream filter passes only with the arguments AMG gives it. The setts move of
    remux.moved_tracks() passes for any offset and rate."""
    base = ["ffmpeg", "-nostdin", "-v", "warning", "-i", clip, "-map", "0:a"]
    for graph in (r"asetpts=print(PTS*TB\,24)", r"asetpts=PTS-STARTPTS+0*print(T)", "aformat=channel_layouts=stereo", "volumedetect=x",
                  "silencedetect=noise=-25dB:d=print(1)", "showinfo=checksum=1", "volumedetect[out]"):
        with pytest.raises(ff.TimeRead, match="does not allow"):
            ff.check(use(), base + ["-af", graph, "-f", "null", "-"])
    for bsf in (r"setts=pts=print(PTS)", "setts=ts=TS", "filter_units=pass_types=1", "aac_adtstoasc=x"):
        with pytest.raises(ff.TimeRead, match="does not allow"):
            ff.check(use("setts"), base + ["-c", "copy", "-bsf:0", bsf, "-f", "null", "-"])
    got = []

    def run(use, argv, **kw):   # the argv of the call, checked, and no ffmpeg
        got.append(ff.check(use, argv)["got"])
        raise KeyError("no run")
    monkeypatch.setattr(ff, "run", run)
    for offset, rate in ((-0.5, "1"), (12.25, "25025/24000"), (1e-07, "1001/1000"), (3600.0, "1/3")):
        with pytest.raises(KeyError):
            sys.modules["arr_media_guard.remux"].moved_tracks([(clip, 2, {"offset": offset, "rate": rate}, "o.mkv", 3)])
    assert got == [{"setts", "matroska"}] * 4


def test_the_info_lines_keep_only_named_channels(clip, use):
    """A5: at level info ffmpeg prints the input dump (Duration, start, Chapter, the DURATION tags) and its last stats
    line (time=). A use keeps the lines of its named channels, the dump's stream lines when it names dump, and the
    lines below info."""
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info", "-ss", "0.5", "-i", clip, "-t", "1", "-map", "0:a",
            "-af", "volumedetect", "-f", "null", "-"]
    for names in (("volumedetect", "seek"), ("volumedetect", "seek", "dump")):
        err = ff.run(use(*names), argv, capture_output=True, text=True).stderr
        assert "max_volume" in err and not re.search(r"Duration|start:|Chapter|DURATION|time=|n_samples", err), err
        assert ("Output #0" in err and "Audio:" in err) == ("dump" in names)
    assert hook.arr_decide.parse_sample(ff.run(use("volumedetect", "seek", "dump"), argv, capture_output=True, text=True).stderr)["rate"] == 44100


def test_debug_output_a_report_and_encoder_stats_fail(clip, use, tmp_path, monkeypatch):
    """A6: a level above info, -report, FFREPORT, -fdebug and the encoder stats files fail unless a use names them."""
    base = ["ffmpeg", "-nostdin", "-i", clip, "-f", "null", "-"]
    for extra in (["-v", "trace"], ["-v", "debug"], ["-v", "verbose"], ["-v", "48"]):
        with pytest.raises(ff.TimeRead, match="level"):
            ff.check(use(), base[:2] + extra + base[2:])
    for extra, ch in ((["-report"], "report"), (["-fdebug", "ts"], "fdebug"), (["-stats_enc_pre", str(tmp_path / "s")], "stats_enc")):
        with pytest.raises(ff.TimeRead, match=ch):
            ff.check(use(), base[:2] + ["-v", "error"] + extra + base[2:])
    with pytest.raises(ff.TimeRead, match="report"):
        ff.check(use(), base[:2] + ["-v", "error"] + base[2:], env={"FFREPORT": f"file={tmp_path}/r.log"})
    monkeypatch.setenv("FFREPORT", f"file={tmp_path}/r.log")
    with pytest.raises(ff.TimeRead, match="report"):
        ff.check(use(), base[:2] + ["-v", "error"] + base[2:])


def test_an_output_with_no_muxer_fails(clip, use, tmp_path):
    """A7: ffmpeg guesses the muxer of an output with no -f from its name: x.srt writes cue times, x.ffmeta chapters."""
    for outs in ([str(tmp_path / "x.srt")], [str(tmp_path / "x.ffmeta")], ["-f", "null", "-", str(tmp_path / "x.srt")]):
        with pytest.raises(ff.TimeRead, match="no -f"):
            ff.check(use(), ["ffmpeg", "-v", "error", "-i", clip, *outs])


@pytest.fixture
def lid_py():
    py = os.path.join(os.environ.get("ARR_LID_DIR", "/opt/arr-media-guard-lid"), "venv", "bin", "python")
    if not (os.path.exists(py) and subprocess.run([py, "-c", "import av"], capture_output=True).returncode == 0):
        pytest.skip("needs a language detection install with PyAV, see ARR_LID_DIR")
    return py


def test_pyav_reads_only_the_attributes_its_use_names(clip, lid_py, tmp_path):
    """In the venv of language detection: a time attribute the use does not name fails, and avwindow.main and
    burnin.Sampler read each attribute their entries name. A9: every object PyAV gives is a proxy, with a repr that
    shows no time. A10: metadata, chapters, a subtitle set and the sample count of a frame fail. A filter graph and a
    resampler fail, and so does to_ndarray() of an audio frame."""
    run = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(ROOT.parent)!r})
        from arr_media_guard import avwindow, burnin, ff
        def tries(what, f):
            try:
                f()
                print("no refusal", what)
            except ff.TimeRead as e:
                print("refused", what)
        av = ff.pyav("burnin.Ocr.read")
        c = av.open({clip!r})
        try:
            c.start_time
            print("no refusal")
        except ff.TimeRead as e:
            print("refused", e)
        p = next(c.demux(c.streams.video[0]))
        for k in ("pts", "dts", "duration", "time"):
            try:
                getattr(p, k)
            except ff.TimeRead:
                print("refused packet." + k)
        tries("container.open", lambda: av.container.open({clip!r}).duration)
        tries("metadata", lambda: c.streams.video[0].metadata)
        tries("chapters", lambda: c.chapters())
        print("repr", repr(p), str(p), "unwrap" in dir(ff))
        a = av.open({clip!r})
        fr = next(a.decode(a.streams.audio[0]))
        tries("samples", lambda: fr.samples)
        rs = av.AudioResampler(format="s16", layout="mono", rate=8000)
        tries("resampler", lambda: rs.resample(fr))
        tries("graph", lambda: av.filter.Graph().add_abuffer(template=a.streams.audio[0]))
        tries("audio frame", lambda: fr.to_ndarray())
        s = av.open({clip!r})
        subs = [x for pk in s.demux(s.streams.subtitles[0]) for x in s.streams.subtitles[0].codec_context.decode(pk)]
        tries("subtitle set", lambda: subs[0].pts)
        print("isinstance", isinstance(fr, av.AudioFrame), isinstance(p, av.AudioFrame))
        avwindow.main({clip!r}, 0, 0.5, 1.0)
        sm = burnin.Sampler({clip!r})
        sm.no_cues(2.0)
        sm.keyframe(1.0)
        sm.frame_at(0.5)
        print(json.dumps({{k: sorted(v) for k, v in ff.USED.items()}}))
    """)
    r = subprocess.run([lid_py, "-c", run], capture_output=True, text=True, env=dict(os.environ, AMG_FF_FAULTS=str(tmp_path / "f.txt")))
    lines = r.stdout.splitlines()
    assert lines[0].startswith("refused burnin.Ocr.read reads container.start_time"), r.stdout + r.stderr[-800:]
    assert lines[1:5] == [f"refused packet.{k}" for k in ("pts", "dts", "duration", "time")]
    assert lines[5:8] == ["refused container.open", "refused metadata", "refused chapters"], r.stdout + r.stderr[-800:]
    assert lines[8] == "repr <Packet for burnin.Ocr.read> <Packet for burnin.Ocr.read> False"
    assert lines[9:14] == ["refused samples", "refused resampler", "refused graph", "refused audio frame", "refused subtitle set"], \
        r.stdout + r.stderr[-800:]   # N9, N10: a filter graph, a resampler and the samples of an audio frame
    assert lines[14] == "isinstance True False"
    used = json.loads(lines[15])
    assert used["avwindow.main"] == sorted(n for n in ff.READS["avwindow.main"].names if n.partition(".")[0] in ff.AV), r.stderr[-500:]
    assert set(ff.READS["burnin.Sampler"].names) - set(used["burnin.Sampler"]) == {"packet.dts"}   # a packet with no pts only


PACKAGE_FILE = str(ROOT / "zz_escape.py")   # code compiled under this name runs as a module of the package would


def test_the_audit_hook_refuses_a_start_or_an_import_outside_ff(clip, use, tmp_path):
    """A11: ff.py's audit hook sees every start of ffmpeg or ffprobe and the import of PyAV. From a module of the
    package other than ff.py each one fails: subprocess, os.system, os.posix_spawn, a launcher, a shell and a Python
    child with no script of the package, which has no guard. Code outside the package, as this test, passes."""
    probe = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", clip]
    for src in (f"subprocess.run({probe!r}, capture_output=True)", f"os.system({' '.join(probe)!r})",
                f"os.posix_spawnp('ffprobe', {probe!r}, os.environ)", f"subprocess.run(['timeout', '9', *{probe!r}])",
                f"subprocess.run({' '.join(probe)!r}, shell=True)", "import av", "subprocess.run([sys.executable, '-c', 'print(1)'])"):
        with pytest.raises(ff.TimeRead, match="zz_escape.py:1"):
            exec(compile(src, PACKAGE_FILE, "exec"), {"subprocess": subprocess, "os": os, "sys": sys})
    assert float(subprocess.run(probe, capture_output=True, text=True).stdout) > 1.9   # a test starts ffprobe itself
    assert len(ff.faults(time.time() - 60)) == 7


def test_the_hook_refuses_a_pool_a_launcher_executable_and_ctypes(clip, use):
    """A thread or a pool that runs subprocess.run() itself has callers in the standard library only, so the start
    counts as one of package code (N3). A launcher before a Python child and executable= fail (N4), and so do a lookup
    of a C function through ctypes and a multiprocessing child (N5). A Python child through ff.run() needs a script of
    the package too."""
    probe = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", clip]
    child = f"import subprocess; subprocess.run({probe!r})"
    pool = "import concurrent.futures\nwith concurrent.futures.ThreadPoolExecutor(1) as ex:\n    "
    shapes = {pool + "ex.submit(subprocess.run, P).result()": "standard library alone, as a thread or a pool, starts ffprobe",
              pool + "list(ex.map(subprocess.check_output, [P]))": "standard library alone, as a thread or a pool, starts ffprobe",
              "subprocess.run(['env', sys.executable, '-c', C])": "zz_escape.py:1 starts env",
              "subprocess.run(['x', '-c', C], executable=sys.executable)": "zz_escape.py:1 starts a Python that runs no script",
              "subprocess.run(['sh', '-c', 'true;' + sys.executable + ' -c pass'])": "zz_escape.py:1 starts sh",
              "os.system('true')": "zz_escape.py:1 starts sh",
              "import ctypes\nctypes.CDLL(None).system(b'true')": "zz_escape.py:2 looks up the C function 'system'"}
    if sys.version_info >= (3, 14):   # 3.13 raises no audit event for the start of a multiprocessing child
        shapes["import multiprocessing\nmultiprocessing.get_context('spawn').Process(target=subprocess.run, args=(P,)).start()"] = \
            "zz_escape.py:2 starts a Python that runs no script"
    for src, said in shapes.items():
        with pytest.raises(ff.TimeRead, match=re.escape(said)):
            exec(compile(src, PACKAGE_FILE, "exec"), {"subprocess": subprocess, "os": os, "sys": sys, "P": probe, "C": child})
    with pytest.raises(ff.TimeRead, match=r"ff.py:\d+ starts a Python that runs no script"):
        ff.run(None, [sys.executable, "-c", child])
    assert len(ff.faults(time.time() - 60)) == len(shapes) + 1


def test_the_hook_and_ff_pass_each_start_of_the_package(tmp_path):
    """The programs the package starts pass from package code: ionice of the process itself, mkvmerge after the
    launchers, and a Python that runs a script of the package. A track name that ends in /ffmpeg is no start of ffmpeg
    (N8), for ff.check() and for the hook."""
    argv = ["mkvmerge", "--version", "--track-name", "0:/a/ffmpeg", "--title", "ffprobe"]
    assert ff.check("checks.ffprobe_audio", argv) is None and hook.run_bounded(argv, 30, capture_output=True).returncode == 0
    src = (f"subprocess.run({argv!r}, capture_output=True, check=True)\n"
           "subprocess.run(['ionice', '-c3', '-p', str(os.getpid())], check=True)\n"
           "subprocess.run(['ionice', '-c3', 'nice', '-n', '19', 'mkvmerge', '--version'], capture_output=True, check=True)\n"
           f"subprocess.run([sys.executable, {str(ROOT / 'lid.py')!r}, '--help'], capture_output=True, check=True)")
    exec(compile(src, PACKAGE_FILE, "exec"), {"subprocess": subprocess, "os": os, "sys": sys})


def test_every_copy_of_the_package_catches_the_refusal_of_the_hook(clip, use):
    """One audit hook runs per process, the one of the first copy of ff.py that loads. Each copy takes the TimeRead and
    the FAULTS of that copy, so a test of any copy catches the refusal of the hook, whichever test file loads first."""
    amg.load("arr_media_guard_late")
    late = sys.modules["arr_media_guard_late.ff"]
    assert late.TimeRead is ff.TimeRead and late.FAULTS is ff.FAULTS
    with pytest.raises(late.TimeRead, match="zz_escape.py:1 starts ffprobe"):
        exec(compile("subprocess.run(['ffprobe', '-version'])", PACKAGE_FILE, "exec"), {"subprocess": subprocess})
    assert [w for t, w in late.FAULTS if "zz_escape" in w]


def test_the_hook_holds_in_a_child_and_in_a_pool_worker(clip, tmp_path):
    """A11: a child that imports the package, and a worker of a process pool, have the hook too. The child's refusal
    names this process as its parent, so faults() counts it here."""
    faults = tmp_path / "f.jsonl"
    code = f"import sys; sys.path.insert(0, {str(ROOT.parent)!r}); import subprocess, arr_media_guard\n" \
           f"exec(compile('subprocess.run([\"ffprobe\", \"-version\"])', {PACKAGE_FILE!r}, 'exec'))"
    started = time.time()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=dict(os.environ, AMG_FF_FAULTS=str(faults)))
    assert "TimeRead" in r.stderr and "zz_escape.py:1 starts ffprobe" in faults.read_text()
    old = os.environ["AMG_FF_FAULTS"]
    os.environ["AMG_FF_FAULTS"] = str(faults)
    try:
        assert [x for x in ff.faults(started) if "zz_escape" in x]
    finally:
        os.environ["AMG_FF_FAULTS"] = old
    with concurrent.futures.ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
        assert pool.submit(ff.names, "lid.extract").result() == frozenset(("seek", "aresample"))   # the worker imports the package
        assert pool.submit(eval, "__import__('sys')._amg_ff_audit").result() is True


@pytest.fixture
def production(settings, tmp_path, monkeypatch):
    """Production settings: a state folder, a decision log, and AMG_FF_FAULTS in the state folder, as config.py names it
    at load, see test_each_entry_names_the_fault_file(). Returns the file it names."""
    state = tmp_path / "state"
    state.mkdir()
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(state))
    monkeypatch.setattr(sys.modules["arr_media_guard.logs"], "to_syslog", lambda line: None)
    monkeypatch.setenv("AMG_FF_FAULTS", str(state / "ff-faults.jsonl"))
    return state / "ff-faults.jsonl"


def test_each_entry_names_the_fault_file(tmp_path):
    """config.py names STATE_DIR/ff-faults.jsonl at load. So the subtitle hunter, which never runs cli.main(), and each
    child it starts write their refusals there too (N6)."""
    env = {k: v for k, v in os.environ.items() if k != "AMG_FF_FAULTS"}
    env.update(STATE_DIR=str(tmp_path), ARR_MEDIA_GUARD_ENV=str(tmp_path / "none.env"))
    for module in ("arr_subhunt", "arr_media_guard.cli"):
        code = f"import os, sys; sys.path.insert(0, {str(ROOT.parent)!r}); import {module}; print(os.environ.get('AMG_FF_FAULTS'))"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert r.stdout.strip() == str(tmp_path / "ff-faults.jsonl"), r.stdout + r.stderr[-500:]


def stored(path):
    """The decision lines of path in the state store, as the nightly audit reads them."""
    return [json.loads(raw) for (raw,) in hook.store.read("SELECT rec FROM decisions ORDER BY rowid") if json.loads(raw).get("path") == path]


def test_a_refusal_a_caller_swallows_errors_the_decision_line(clip, production, monkeypatch, capsys):
    """B1, review B's p1: a new unnamed read in proof.ff_streams raises TimeRead, and burn_probe() takes it as no
    track. The refusal leaves one line in the state folder, and the decision line of the run is an error that names
    the use and the field, which the nightly audit counts."""
    monkeypatch.setitem(ff.READS, "proof.ff_streams", ff.Use(("stream.duration",), "format.duration left out"))
    started = time.time()
    assert hook.burn_probe(clip, {})[1:] == (False, 0.0)
    rec = hook.decision(dict(app="radarr", source="hook", path=clip, result="no change", outcome="no_change"), started)
    lines = [json.loads(x) for x in production.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["pid"] == os.getpid() and "proof.ff_streams (format) reads duration" in lines[0]["what"]
    assert rec["outcome"] == "error" and "TimeRead: proof.ff_streams (format) reads duration" in rec["result"]
    assert [(r["outcome"], r["result"]) for r in stored(clip)] == [("error", rec["result"])]


def test_a_refusal_in_a_child_errors_the_decision_line(clip, lid_py, production, settings, tmp_path, monkeypatch):
    """B1, review B's p2: the PyAV window with frame.pts left out of its entry. The child avwindow.py refuses, the
    window does not run, and the parent's decision line is an error that names the child's refusal."""
    copy = tmp_path / "pkg" / "arr_media_guard"
    copy.mkdir(parents=True)
    for name in ("avwindow.py", "ff.py"):
        shutil.copy(ROOT / name, copy / name)
    text = (copy / "ff.py").read_text()
    entry = '"avwindow.main": Use(("container.start_time", "container.seek", "frame.pts", '
    assert entry in text
    (copy / "ff.py").write_text(text.replace(entry, entry.replace(' "frame.pts",', "")))
    settings(lid_dir=os.path.dirname(os.path.dirname(os.path.dirname(lid_py))))
    monkeypatch.setattr(sys.modules["arr_media_guard.checks"], "__file__", str(copy / "checks.py"))
    for m in [m for k, m in list(sys.modules.items()) if k.endswith(".ff")]:   # the hook of each copy takes the copy's script
        monkeypatch.setattr(m, "HOMES", m.HOMES | {str(copy)})
    started = time.time()
    w = sys.modules["arr_media_guard.checks"].window(clip, 0.5, 1.0, stream=0)
    assert (w["frames"], w["ran"], w["via"]) == (0, False, "pyav")
    rec = hook.decision(dict(app="radarr", source="hook", path=clip, result="no change", outcome="no_change"), started)
    lines = [json.loads(x) for x in production.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["ppid"] == os.getpid() and "avwindow.main reads frame.pts" in lines[0]["what"]
    assert rec["outcome"] == "error" and "avwindow.main reads frame.pts" in rec["result"] and stored(clip)[0]["outcome"] == "error"
