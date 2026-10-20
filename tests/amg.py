# arr-media-guard, a Sonarr and Radarr import hook that sets default tracks and catches broken files.
# Copyright (C) 2026 samwiseg0
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""The package as a test sees it: one object for the names the script held when it was one file. load() gives a test
file its own copy of the package, as loading the script by path did. ffmpeg() makes a test clip once and copies it
after that."""
import contextlib
import functools
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "arr_media_guard")
SCRIPT = ("apps", "checks", "cli", "config", "convert", "judge", "logs", "plex", "process", "proof", "regrab", "remux", "report", "runner",
          "subtitles", "vault")   # the modules the script held, report.py and judge.py
ALIASES = {"arr_decide": "decide", "arr_meta": "content", "arr_status": "health", "arr_subsync": "subsync", "arr_lid": "lid",
           "arr_serve": "serve"}   # the names of the modules beside the script
sys.path.insert(0, ROOT)   # for arr_subhunt.py


class Package:
    """A read of a name gives the value in the module that defines it. A write goes to that module, and a patch reaches
    every caller, because the modules call each other through the module. A name that only imports bind, such as time,
    is written into each module that binds it. A name that two modules hold raises, because one of them imported it
    from the other, and a patch would miss its callers. The modules beside the script keep their old names, as
    hook.arr_decide."""

    def __init__(self, name):
        mods = [sys.modules[f"{name}.{m}"] for m in SCRIPT]
        held = lambda module: {k for m in mods for k, v in vars(m).items() if isinstance(v, types.ModuleType) == module}
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_mods", mods)
        object.__setattr__(self, "_imports", held(True) - held(False))   # the names only imports bind

    __file__ = os.path.join(ROOT, "arr-media-guard")   # the launcher, the path the apps run

    def _where(self, key):
        found = [m for m in self._mods if key in vars(m)]
        if key in self._imports:
            return found
        defs = [m for m in found if not isinstance(vars(m)[key], types.ModuleType)]
        if len(defs) > 1:
            raise AttributeError(f"{key} is in {', '.join(m.__name__ for m in defs)}. A module imported it from another one.")
        return defs

    def __getattr__(self, key):
        if key in ALIASES:
            return sys.modules[f"{self._name}.{ALIASES[key]}"]
        where = self._where(key)
        if not where:
            raise AttributeError(f"the package defines no {key}")
        return getattr(where[0], key)

    def __setattr__(self, key, value):
        where = self._where(key)
        if not where:
            raise AttributeError(f"the package defines no {key}")
        for m in where:
            setattr(m, key, value)


def load(name="arr_media_guard"):
    """A Package of the package loaded as name. arr_media_guard is the package itself, which arr_subhunt.py imports.
    Another name loads a new copy, with the settings of the env file at that moment and its own state."""
    if name != "arr_media_guard":
        for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            del sys.modules[k]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, os.path.join(PACKAGE, "__init__.py"), submodule_search_locations=[PACKAGE])
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    for m in SCRIPT + tuple(ALIASES.values()):
        importlib.import_module(f"{name}.{m}")
    return Package(name)


def deadlines():
    """The job's time limit, content.DEADLINE, of each copy of the package loaded so far."""
    return [m.DEADLINE for k, m in list(sys.modules.items()) if k.endswith(".content") and hasattr(m, "DEADLINE")]


def stops():
    """The stop state, runner.STOP, of each copy of the package loaded so far."""
    return [m.STOP for k, m in list(sys.modules.items()) if k.endswith(".runner") and hasattr(m, "STOP")]


MEDIA = os.environ.get("AMG_TEST_MEDIA") or os.path.join(os.path.expanduser("~"), ".cache", "arr-media-guard", "test-media")
MEDIA = MEDIA if MEDIA == "off" else os.path.abspath(MEDIA)   # a test that changes folder keeps the same cache
STOPS = r"\s:,'|=\[\];"   # the characters that end a path inside an argument
INNER_PATH = re.compile(rf"/[^{STOPS}]+")   # an absolute path inside an argument, as subtitles=/a/b.srt or concat:/a|/b
WORD = re.compile(rf"[^{STOPS}]+")
KEPT = re.compile(r"[0-9a-f]{64}(\.\w+)?|\.new-\w+")   # the names of the clips and temp files ffmpeg() writes
RUN = subprocess.run   # bound at import, so a test that fakes subprocess.run never reaches the cache


@functools.cache
def ffmpeg_version():
    """The part of the key that the host decides. It holds the output of ffmpeg -version, the size and mtime of the
    ffmpeg program and of each library ldd lists for it, and each line of fc-list with the size and mtime of its font
    file. An update of a codec library, libass or a font changes it. None when ldd or fc-list is missing, and then
    ffmpeg() keeps no clip."""
    stat = lambda path: f"{path} {os.stat(path).st_size} {os.stat(path).st_mtime_ns}"
    try:
        version = RUN(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout
        libs = re.findall(r"(/\S+) \(0x", RUN(["ldd", shutil.which("ffmpeg")], capture_output=True, text=True).stdout)
        fonts = sorted(RUN(["fc-list"], capture_output=True, text=True, check=True).stdout.splitlines())
        return "\n".join([version, *map(stat, [shutil.which("ffmpeg"), *libs]), *(f"{f} {stat(f.split(': ')[0])}" for f in fonts)])
    except FileNotFoundError:
        return None


@functools.cache
def prune(folder, days=3):
    """Deletes each clip and temp file of ffmpeg() in folder that no call has used for days, once in each process. A
    clip made from a file that differs on each run, such as one mkvmerge wrote with new UIDs, is never used again."""
    old = time.time() - days * 86400
    for f in os.scandir(folder):
        with contextlib.suppress(FileNotFoundError):   # another worker renamed or deleted it after the scan
            if KEPT.fullmatch(f.name) and f.stat().st_mtime < old:
                os.remove(f.path)


def content(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest() + os.path.splitext(path)[1]


def key(argv):
    """The key of the ffmpeg call argv, or None when the call reads a file the key cannot see. The key holds
    ffmpeg_version(), the arguments before the output and the output's extension. An argument that names a file counts
    by the file's content. So does each absolute path inside an argument, as in subtitles=/a/b.srt, and each character
    in STOPS ends such a path. A concat list, -attach and an input that is neither a file nor a lavfi graph, such as an
    image pattern, give None. So do a relative path inside an argument, and a path inside an argument that names no
    file in a folder that exists, such as a path with a space."""
    version, fmt, parts = ffmpeg_version(), None, []
    if version is None or "-attach" in argv:
        return None
    for flag, a in zip([None, *argv], map(str, argv[:-1])):
        inner = INNER_PATH.findall(a)
        unseen = [p for p in inner if not os.path.isfile(p)]
        if flag == "-i":
            if fmt == "concat" or (fmt != "lavfi" and not os.path.isfile(a) and (unseen or not inner)):
                return None
            fmt = None
        fmt = a if flag == "-f" else fmt
        if os.path.isfile(a):
            parts.append(content(a))
            continue
        if any(os.path.dirname(p) != "/" and os.path.isdir(os.path.dirname(p)) for p in unseen) or any(
                not w.startswith("/") and os.path.isfile(w) for w in WORD.findall(a)):
            return None
        parts.append(INNER_PATH.sub(lambda m: content(m[0]) if os.path.isfile(m[0]) else m[0], a))
    return json.dumps([version, parts, os.path.splitext(str(argv[-1]))[1]])


def ffmpeg(argv, **kw):
    """subprocess.run() for an ffmpeg call that makes a test clip, the file its last argument names. The first call with
    a key, see key(), makes the clip and keeps a copy in the folder AMG_TEST_MEDIA names. A later call with that key
    copies the clip. Each call writes its own copy, so a test may change it. When a call keeps a new clip, it deletes
    each clip that no call used for 3 days, see prune(). AMG_TEST_MEDIA=off makes every clip again. A call runs as it
    is when key() gives None, when it runs another program, or when it passes an argument other than check. Only a
    test's own clips come here. Product code, and a test that runs a command line of the product, run the real tools."""
    out = str(argv[-1])
    k = None if MEDIA == "off" or argv[0] != "ffmpeg" or set(kw) - {"check"} or out == "-" else key(argv)
    if k is None:
        return RUN(argv, **kw)
    kept = os.path.join(MEDIA, hashlib.sha256(k.encode()).hexdigest() + os.path.splitext(out)[1])
    with contextlib.suppress(FileNotFoundError):   # none kept, or another run pruned it just now
        os.utime(kept)
        shutil.copyfile(kept, out)
        return subprocess.CompletedProcess(argv, 0)
    made = RUN(argv, **kw)
    if made.returncode == 0:   # a temp name, then a rename, so two workers that make the same clip at once both keep a whole file
        os.makedirs(MEDIA, exist_ok=True)
        prune(MEDIA)
        fd, tmp = tempfile.mkstemp(dir=MEDIA, prefix=".new-")
        os.close(fd)
        shutil.copyfile(out, tmp)
        os.replace(tmp, kept)
    return made
