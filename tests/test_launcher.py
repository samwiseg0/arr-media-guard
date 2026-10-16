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
"""Unit tests for the two launchers, the package layout and the clip cache of the tests. Each test runs real programs.

Run: pytest tests/test_launcher.py
"""
import os
import re
import shutil
import subprocess
import sys
import time

import pytest

import amg

REAL_RUN = subprocess.run   # the cache tests fake it


def test_each_module_imports_first():
    """The modules import each other. Each one must import first in a fresh interpreter, so no module reads another one
    at import time before that one has run."""
    names = sorted(n[:-3] for n in os.listdir(amg.PACKAGE) if n.endswith(".py") and n != "__init__.py")
    env = dict(os.environ, ARR_MEDIA_GUARD_ENV="/nonexistent/arr-media-guard.env")
    runs = {n: subprocess.Popen([sys.executable, "-c", f"import arr_media_guard.{n}"], cwd=amg.ROOT, env=env, stderr=subprocess.PIPE,
                                text=True) for n in names}
    failed = {n: p.communicate()[1][-300:] for n, p in runs.items() if p.wait()}
    assert len(names) >= 20 and not failed, failed


def test_the_launchers_run_by_path_and_through_a_symlink(tmp_path):
    """A Custom Script connection runs a symlink to the launcher. The launcher finds the package beside its real path."""
    (tmp_path / "env").write_text(f"POLICY_FILE='{os.path.join(amg.ROOT, 'examples', 'policy.json')}'\nSTATE_DIR='{tmp_path}'\n"
                                  f"LOG='{tmp_path}/log.jsonl'\nSONARR_DIR='/nonexistent'\nRADARR_DIR='/nonexistent'\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("sonarr_", "radarr_", "ARR_MEDIA_GUARD"))}
    env["ARR_MEDIA_GUARD_ENV"] = str(tmp_path / "env")
    for name in ("arr-media-guard", "arr-media-guard-subhunt"):
        (tmp_path / name).symlink_to(os.path.join(amg.ROOT, name))
    for where in (amg.ROOT, str(tmp_path)):
        run = lambda name, *args, **more: subprocess.run([sys.executable, os.path.join(where, name), *args], env=dict(env, **more),
                                                         capture_output=True, text=True, timeout=60)
        r = run("arr-media-guard", "--help")
        assert r.returncode == 0 and "arr-media-guard --selftest" in r.stdout, r.stderr
        r = run("arr-media-guard", "--subhunt", "radarr", "--ids", "1")
        assert r.returncode == 2 and "arr-media-guard-subhunt" in r.stderr
        r = run("arr-media-guard", sonarr_eventtype="Test")   # the hook's Test asks the app, and no app runs here
        assert (r.returncode, r.stdout) == (1, "") and r.stderr.startswith("arr-media-guard: the Sonarr API did not answer: "), r.stderr
        r = run("arr-media-guard", sonarr_eventtype="Grab")
        assert (r.returncode, r.stdout) == (0, "arr-media-guard: Grab ok\n"), r.stderr
        r = run("arr-media-guard-subhunt", "--help")
        assert r.returncode == 0 and r.stdout.startswith("usage: arr-media-guard-subhunt"), r.stderr


def test_language_detection_runs_by_path():
    """The lid venv runs lid.py by its path, as in lid_cli() and the image's lid.py --fetch."""
    r = subprocess.run([sys.executable, os.path.join(amg.PACKAGE, "lid.py"), "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--fetch" in r.stdout, r.stderr


def test_the_help_of_each_command_needs_no_policy_and_names_the_instances(tmp_path):
    """--backfill --help prints its options when the policy does not load. The hunter's help offers the Radarr
    instances only, and says what each flag does."""
    (tmp_path / "env").write_text(f"POLICY_FILE='{tmp_path}/missing.json'\nSTATE_DIR='{tmp_path}'\nLOG='{tmp_path}/log.jsonl'\n"
                                  "SONARR_DIR='/nonexistent'\nRADARR_DIR='/nonexistent'\nAPP_INSTANCES='sonarr-4k:sonarr,radarr-4k:radarr'\n"
                                  "SONARR_4K_URL='http://127.0.0.1:1'\nRADARR_4K_URL='http://127.0.0.1:1'\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("sonarr_", "radarr_", "ARR_MEDIA_GUARD"))}
    env["ARR_MEDIA_GUARD_ENV"] = str(tmp_path / "env")
    run = lambda *args: subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, timeout=60)
    r = run(os.path.join(amg.ROOT, "arr-media-guard"), "--backfill", "--help")
    assert r.returncode == 0 and r.stdout.startswith("usage: arr-media-guard --backfill") and "{radarr,radarr-4k,sonarr,sonarr-4k}" in r.stdout, r
    r = run(os.path.join(amg.ROOT, "arr-media-guard"), "--backfill", "radarr")
    assert r.returncode == 1 and "missing.json" in r.stderr, r   # without --help the policy still stops it
    r = run(os.path.join(amg.ROOT, "arr-media-guard"), "--help")
    assert "--backfill <instance>" in r.stdout and "<radarr|sonarr>" not in r.stdout, r.stdout
    r = run(os.path.join(amg.ROOT, "arr-media-guard-subhunt"), "--help")
    assert r.returncode == 0 and "{radarr,radarr-4k}" in r.stdout and "English subtitle" in r.stdout, r
    text = " ".join(r.stdout.split())
    assert all(h in text for h in ("the Radarr movie ids", "download, check and import", "hunt again for a movie")), r.stdout
    r = run(os.path.join(amg.ROOT, "arr-media-guard-subhunt"), "sonarr-4k", "--ids", "1")
    assert r.returncode == 2 and "invalid choice: 'sonarr-4k'" in r.stderr, r


SRT = "1\n00:00:00,000 --> 00:00:00,400\nOne\n"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """(the cache folder, the names of the clips ffmpeg made) for a test of amg.ffmpeg(). The key holds "v1" in place of
    ffmpeg_version(). A test fake of subprocess.run fails the test when amg.ffmpeg() reaches it."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    kept, made, real = tmp_path / "kept", [], amg.RUN
    kept.mkdir()
    monkeypatch.setattr(amg, "MEDIA", str(kept))
    monkeypatch.setattr(amg, "RUN", lambda argv, **kw: made.append(os.path.basename(argv[-1])) or real(argv, **kw))
    monkeypatch.setattr(amg, "ffmpeg_version", lambda: "v1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("amg.ffmpeg() ran a test fake of subprocess.run"))
    return kept, made


def test_a_test_clip_is_made_once_for_each_key(tmp_path, cache, monkeypatch):
    """amg.ffmpeg() makes a clip once and copies it after that, and a copy marks the clip used. A changed argument, a
    changed input file, a changed file in a filter and a changed ffmpeg_version() each make a new clip. A test that
    changes its copy leaves the kept clip as it was. A call that fails or reads its own output keeps nothing.
    AMG_TEST_MEDIA=off makes each clip."""
    kept, made = cache
    srt, burn = tmp_path / "s.srt", tmp_path / "burn.srt"
    srt.write_text(SRT), burn.write_text(SRT)
    clip = lambda name, freq=440, codec="aac", **kw: amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                                                 f"sine=frequency={freq}:duration=0.5", "-i", str(srt), "-c:a", codec,
                                                                 "-c:s", "srt", str(tmp_path / name)], **kw)
    clip("a.mkv", check=True)
    (first,) = os.listdir(kept)
    os.utime(kept / first, (time.time() - 2 * 86400,) * 2)
    clip("b.mkv", check=True)
    assert made == ["a.mkv"] and (tmp_path / "b.mkv").read_bytes() == (tmp_path / "a.mkv").read_bytes()
    assert time.time() - os.path.getmtime(kept / first) < 60
    (tmp_path / "b.mkv").write_bytes(b"changed by its test")
    clip("c.mkv", check=True)
    assert (tmp_path / "c.mkv").read_bytes() == (tmp_path / "a.mkv").read_bytes() and made == ["a.mkv"]
    clip("d.mkv", freq=880, check=True)
    srt.write_text(SRT.replace("One", "Two"))
    clip("e.mkv", check=True)
    burnt = lambda name: amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=64x64:duration=0.5", "-vf", f"subtitles={burn}",
                                     str(tmp_path / name)], check=True)
    burnt("f.mkv"), burnt("g.mkv")
    burn.write_text(SRT.replace("One", "Two"))
    burnt("h.mkv")
    monkeypatch.setattr(amg, "ffmpeg_version", lambda: "v2")
    clip("i.mkv", check=True)
    assert made == ["a.mkv", "d.mkv", "e.mkv", "f.mkv", "h.mkv", "i.mkv"] and len(os.listdir(kept)) == 6
    told, failed = clip("j.mkv", capture_output=True), clip("k.mkv", codec="nosuch")
    assert told.stderr == b"" and failed.returncode and made[-2:] == ["j.mkv", "k.mkv"] and len(os.listdir(kept)) == 6
    monkeypatch.setattr(amg, "MEDIA", "off")
    clip("l.mkv", check=True)
    assert made[-1] == "l.mkv" and len(os.listdir(kept)) == 6


def test_a_call_the_key_cannot_see_runs_each_time(tmp_path, cache, monkeypatch):
    """amg.ffmpeg() keeps no clip of a call that reads a file the key cannot see, so that call runs each time: a concat
    list, an image pattern, -attach, a relative path in a filter and a path with a space. A path that a filter label
    or a semicolon ends counts by its content."""
    kept, made = cache
    wav = tmp_path / "a.wav"
    sine = lambda freq: REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=0.5", str(wav)], check=True)
    sine(440)
    for n in (1, 2):
        REAL_RUN(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=32x32", "-frames:v", "1", str(tmp_path / f"f{n:02d}.png")], check=True)
    (tmp_path / "list.txt").write_text(f"file '{wav}'\n")
    (tmp_path / "s.srt").write_text(SRT), (tmp_path / "My Clip.srt").write_text(SRT)
    monkeypatch.chdir(tmp_path)
    video = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=32x32:duration=0.5"]
    unseen = {"concat.wav": ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp_path / "list.txt")],
              "pattern.mkv": ["ffmpeg", "-v", "error", "-y", "-i", "f%02d.png"],
              "attach.mkv": [*video, "-attach", str(tmp_path / "s.srt"), "-metadata:s:t", "mimetype=text/plain"],
              "relative.mkv": [*video, "-vf", "subtitles=s.srt"],
              "space.mkv": [*video, "-vf", f"subtitles='{tmp_path / 'My Clip.srt'}'"]}
    for name, argv in unseen.items():
        amg.ffmpeg([*argv, str(tmp_path / name)], check=True), amg.ffmpeg([*argv, str(tmp_path / name)], check=True)
    assert made == [n for n in unseen for _ in (1, 2)] and os.listdir(kept) == []
    mix = lambda name: amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.5", "-filter_complex",
                                   f"amovie={wav}[b];[0:a][b]amix", str(tmp_path / name)], check=True)
    mix("m1.wav"), mix("m2.wav")
    sine(880)
    mix("m3.wav")
    assert made[-2:] == ["m1.wav", "m3.wav"] and len(os.listdir(kept)) == 2


def test_prune_deletes_only_old_clips_and_temp_files(tmp_path, monkeypatch):
    """prune() deletes each clip and temp file of amg.ffmpeg() that no call used for 3 days. Another file, a folder and
    a newer clip stay. A clip that another worker deletes during the scan is no error."""
    days = {"a" * 64 + ".mkv": 4, "b" * 64 + ".mkv": 2, "c" * 64: 4, "d" * 64 + ".mp4": 4, ".new-x1y2": 4, "notes.txt": 4, "old": 4}
    for name, age in days.items():
        if name == "old":
            (tmp_path / name).mkdir()
        else:
            (tmp_path / name).write_bytes(b"")
        os.utime(tmp_path / name, (time.time() - age * 86400,) * 2)
    scan = os.scandir

    def scandir(folder):   # another worker deletes a clip after the scan
        entries = list(scan(folder))
        (tmp_path / ("d" * 64 + ".mp4")).unlink()
        return entries
    monkeypatch.setattr(os, "scandir", scandir)
    amg.prune(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == sorted(["b" * 64 + ".mkv", "notes.txt", "old"])


def test_the_key_holds_the_libraries_and_the_fonts(monkeypatch, request):
    """ffmpeg_version() holds the version line, and each library of ffmpeg and each font with the size and mtime of its
    file, so an update of one makes new clips. A test fake of subprocess.run never reaches it."""
    if not (shutil.which("ffmpeg") and shutil.which("ldd") and shutil.which("fc-list")):
        pytest.skip("needs ffmpeg, ldd and fc-list")
    lib = re.search(r"=> (/\S*libavcodec\S*)", REAL_RUN(["ldd", shutil.which("ffmpeg")], capture_output=True, text=True).stdout)[1]
    font = min(REAL_RUN(["fc-list"], capture_output=True, text=True, check=True).stdout.splitlines())
    stat = lambda path: f"{path} {os.stat(path).st_size} {os.stat(path).st_mtime_ns}"
    request.addfinalizer(amg.ffmpeg_version.cache_clear)
    amg.ffmpeg_version.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    lines = amg.ffmpeg_version().splitlines()
    assert lines[0].startswith("ffmpeg version ") and stat(lib) in lines and f"{font} {stat(font.split(': ')[0])}" in lines
