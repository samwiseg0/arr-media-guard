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
"""Unit tests for arr-media-guard: the decision module, the hook, its queue worker and the backfill.

No network, no mkvtoolnix. The package is loaded as hook, see amg.py, and run with fake radarr_* and sonarr_*
variables. The tests monkeypatch fork, the app API, mkvmerge, mkvpropedit, Plex, Discord and the
clock. The worker lock and the file lock are real flock locks in a temp directory. The job process tests
fork real processes and trace them through a shared file.

Run: pytest tests/test_arr_media_guard.py
"""
import collections
import concurrent.futures
import contextlib
import copy
import dataclasses
import datetime
import fcntl
import gc
import hashlib
import io
import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import struct
import sys
import threading
import time
import types
from fractions import Fraction
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

import amg

FILES = os.path.join(os.path.dirname(__file__), "..")
os.environ["ARR_MEDIA_GUARD_LIB"] = FILES
os.environ["ARR_MEDIA_GUARD_ENV"] = "/nonexistent/arr-media-guard.env"
hook = amg.load("arr_media_guard_hook")
hook.CFG = dataclasses.replace(hook.CFG, plex_url="https://plex.invalid:32400")   # the fake Plex answers only this host
REAL_FLOCK = fcntl.flock
REAL_MKVMERGE, REAL_WINDOW = hook.mkvmerge, hook.window   # the env fixture fakes them, a test with real files needs them
REAL_FORK, REAL_EXIT, REAL_RUN = os.fork, os._exit, subprocess.run   # the fixture fakes them, the job process tests need them
# The policy is the example policy file.
with open(os.path.join(FILES, "examples", "policy.json")) as _f:
    POLICY = json.load(_f)
hook.arr_decide.set_policy(POLICY)
hook.CFG = dataclasses.replace(hook.CFG, keep_days=0)   # a repack drops its original. The tests of the kept original switch it on.
# mkvmerge's language table as `mkvmerge --list-languages` prints it, so no test runs mkvmerge for it
LANGUAGES = ("English | eng | eng | en\nSpanish | spa | spa | es\nFrench | fre | fre | fr\nJapanese | jpn | jpn | ja\n"
             "Portuguese | por | por | pt\nChinese | chi | chi | zh\nUndetermined | und | und |\n")
hook.LANGS[:] = [hook.arr_decide.language_table(LANGUAGES)]


def noise(n, seed=20260928):
    """n bytes that look random but are the same on every run, so a damaged test file breaks the same way each time."""
    return random.Random(seed + n).randbytes(n)


def mk(typ, lang, uid, dflt, **kw):
    return {"type": typ, "properties": dict(language=lang, uid=uid, default_track=dflt, forced_track=False, **kw)}


PORTUGUESE_DEFAULT = {"container": {"properties": {"duration": 7200 * 10**9}},
                      "tracks": [mk("video", "und", 1, True, pixel_dimensions="1920x1080"), mk("audio", "por", 2, True, audio_channels=2),
                                 mk("audio", "eng", 3, False, audio_channels=6), mk("subtitles", "por", 4, True)]}
NO_ENGLISH = {"container": {"properties": {"duration": 7200 * 10**9}},
              "tracks": [mk("video", "und", 1, True, pixel_dimensions="1920x1080"), mk("audio", "por", 2, True, audio_channels=2)]}


# A clean window, and one with decode errors after its first frame (arr_decide.parse_window() fields)
CLEAN_WINDOW = {"frames": 120, "errors": 0, "gap": 0.04, "late": False, "empty": False, "noisy": False, "cut": False, "ran": True}
BAD_WINDOW = dict(CLEAN_WINDOW, errors=4)


def plex_item(key, guid, path):
    return {"ratingKey": key, "Guid": [{"id": guid}], "Media": [{"Part": [{"file": path}]}]}


@pytest.fixture
def env(tmp_path, monkeypatch, settings):
    """A fake host: one mkv file, the app API, mkvtoolnix, Plex, Discord and a clock. Returns the recorded calls."""
    media = tmp_path / "media" / "Film A (1979)"
    media.mkdir(parents=True)
    path = media / "Film A (1979) WEBDL-1080p.mkv"
    path.write_bytes(b"x" * 1000)
    (tmp_path / "state").mkdir()
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path / "state"), discord_webhook="https://discord.invalid/ops",
             plex_token="plex-t0ken-1234")
    calls = {"mkvpropedit": [], "http": [], "probe": copy.deepcopy(PORTUGUESE_DEFAULT), "files": {}, "path": str(path), "exit": [],
             "events": [], "run_kw": [], "sleeps": [], "forks": 0, "clock": [time.time()], "attempts": [], "plex_misses": 0,
             "movies": {"movie/7": {"title": "Film A", "year": 1979, "originalLanguage": {"name": "English"}, "runtime": 120,
                                    "tmdbId": 90001, "imdbId": "tt9000001"}, "qualityprofile": [{"id": 8, "name": "Kids"}]},
             "plex_items": [plex_item("7101", "tmdb://90001", str(path))], "writes": [], "ffmpeg": [],
             "ffmpeg_out": ["[x] [info] n_samples: 1922128\n[x] [info] max_volume: -4.0 dB\n"], "syslog": [],
             "repacks": [], "durations": {False: 600.0, True: 600.02}, "mkv_probe": copy.deepcopy(NEW_MKV), "activities": [], "checks": [],
             "windows": [], "window_out": [CLEAN_WINDOW],
             "proof": (None, [{"stream": "video 0", "codec": "h264", "method": "packets", "count": 10, "hash": "0" * 16, "match": True}])}
    start = calls["clock"][0]

    def fake_run(argv, **kw):   # ffmpeg, ffprobe, mkvpropedit and a repack reach subprocess.run, mkvmerge -J is patched below
        if "mkvmerge" in argv:   # a repack, ionice -c3 nice -n 19 mkvmerge -q -o <temp> <file>. MKV! marks the Matroska copy.
            calls["repacks"].append(argv); calls["events"].append("repack")
            with open(argv[-1], "rb") as f, open(argv[argv.index("-o") + 1], "wb") as g:
                g.write(b"MKV!" + f.read())
            calls.get("during_repack", lambda: None)()   # what the app does to the original while mkvmerge runs
            return type("R", (), {"returncode": calls.get("repack_rc", 0), "stdout": calls.get("repack_out", ""), "stderr": ""})()
        if argv[0] == "ionice" and "ffmpeg" in argv:   # convert_captions(): the caption SubRip from calls["cc_text"]
            with open(argv[-1], "w") as f:
                f.write(calls.get("cc_text", ""))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if argv[0] == "ionice":   # the backfill and the audit lower their own I/O priority
            return None
        if argv[0] == "ffprobe" and "-show_streams" in argv:   # convert() looks for caption tracks: the fake file's tracks, plus calls["extra_streams"]
            probe = calls["files"].get(argv[-1], calls["probe"])
            kinds = {"video": ("video", "h264"), "audio": ("audio", "aac"), "subtitles": ("subtitle", "mov_text")}
            streams = [{"index": i, "codec_type": kinds[t["type"]][0], "codec_name": kinds[t["type"]][1]} for i, t in enumerate(probe.get("tracks", []))]
            out = json.dumps({"streams": streams + calls.get("extra_streams", []), "format": {"format_name": "mov,mp4", "duration": "600.0"}})
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        if argv[0] == "ffprobe" and "format=duration" in argv:   # the repack compares durations, from calls["durations"]
            with open(argv[-1], "rb") as f:
                return type("R", (), {"returncode": 0, "stdout": f'{calls["durations"][f.read(4) == b"MKV!"]}\n', "stderr": ""})()
        if argv[0] == "ffprobe":   # as many audio streams as the fake mkvmerge lists, unless a test says otherwise
            probe = calls["files"].get(argv[-1], calls["probe"])
            count = sum(t.get("type") == "audio" for t in probe.get("tracks", []))
            out = calls.get("ffprobe_out", "".join(f"{i}\n" for i in range(count)))
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        if argv[0] == "ffmpeg":
            out = calls["ffmpeg_out"][len(calls["ffmpeg"]) % len(calls["ffmpeg_out"])]
            calls["ffmpeg"].append(argv)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": out})()
        assert argv[0] == "mkvpropedit"
        calls["mkvpropedit"].append(argv[2:]); calls["run_kw"].append(kw); calls["events"].append("mkvpropedit")
        with open(hook.CFG.log) as f:   # the undo record is on disk before the file changes. Job processes share the log.
            assert [r["result"] for r in map(json.loads, f) if r.get("path") == argv[1]][-1] == "editing"
        for i in range(2, len(argv), 4):
            (uid, (name, flag)) = int(argv[i + 1].split("=")[1]), (argv[i + 3].split("=") if argv[i + 2] == "--set" else (argv[i + 3], None))
            for t in calls["files"].setdefault(argv[1], copy.deepcopy(calls["probe"]))["tracks"]:
                if t["properties"]["uid"] == uid and name.startswith("language"):   # both tags, the legacy one from mkvmerge's table
                    t["properties"].update(language_ietf=flag, **({} if name == "language-ietf" else {"language": hook.langs()[0].get(flag, flag)}))
                    if flag is None: del t["properties"]["language_ietf"]   # --delete language-ietf
                elif t["properties"]["uid"] == uid:
                    t["properties"][{"flag-default": "default_track", "flag-forced": "forced_track"}[name]] = bool(int(flag))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def fake_http(url, method="GET", body=None, headers=None, timeout=15):
        """Plex with one movie section. It lists nothing for the first plex_misses lookups."""
        calls["http"].append((method, url, body))
        u = urlparse(url)
        if not u.netloc.startswith("plex.invalid"): return b""
        calls["events"].append("plex")
        if u.path == "/library/sections":
            calls["attempts"].append(calls["clock"][0] - start)
            return {"MediaContainer": {"Directory": [{"key": "12", "Location": [{"path": str(tmp_path / "media")}]}]}}
        if u.path == "/library/sections/12/all":
            assert parse_qs(u.query)["includeGuids"] == ["1"]
            return {"MediaContainer": {"Metadata": [] if len(calls["attempts"]) <= calls["plex_misses"] else calls["plex_items"]}}
        if u.path == "/activities":   # one scripted answer per check, then idle. An exception is raised.
            calls["checks"].append(calls["clock"][0] - start)
            act = calls["activities"].pop(0) if calls["activities"] else []
            if isinstance(act, Exception): raise act
            return {"MediaContainer": {"Activity": act}}
        if u.path == "/:/prefs":   # calls["prefs"] is the watcher setting's value, or a list of one per read. None leaves it
            v = calls.get("prefs")   # out. An exception is raised.
            if isinstance(v, list): v = v.pop(0)
            if isinstance(v, Exception): raise v
            return {"MediaContainer": {"size": 177, "Setting": [] if v is None else [dict(WATCH_SETTING, value=v)]}}
        assert method == "PUT" and u.path.endswith("/analyze"), url   # never a section or folder scan
        return b""

    def fake_flock(f, op):
        calls["events"].append("lock" if f.name.endswith("/lock") else "status lock" if f.name.endswith("status.json.lock")
                               else "gate" if f.name.endswith("/lock.gate") else "worker lock")
        REAL_FLOCK(f, op)

    def fake_fork():
        calls["forks"] += 1
        return 0   # run the worker side in-process

    def fake_sleep(s):
        calls["sleeps"].append(s); calls["clock"][0] += s

    def fake_exit(code):
        calls["exit"].append(code)
        raise SystemExit(code)

    def fake_arr(app, p):   # the app's API. parse? and qualityprofile/ answer with no custom format unless a test says other.
        if p.startswith("parse?"):
            return calls.get("parse_cf", lambda title: {})(parse_qs(p.split("?", 1)[1])["title"][0])
        if p.startswith("qualityprofile/"):
            return calls.get("profile", {})
        if p.startswith("moviefile/") and p not in calls["movies"]:   # the record a movie/<id> names, when a test set no other
            return next((m["movieFile"] for k, m in calls["movies"].items() if k.startswith("movie/") and isinstance(m, dict)
                         and (m.get("movieFile") or {}).get("id") == int(p.split("/")[1])), {})
        if p.startswith("extrafile?movieId="):   # Radarr's extra files of the movie, from calls["extra_rows"] as (path, file id, type)
            return [{"relativePath": r, "movieFileId": f, "type": t} for r, f, t in calls.get("extra_rows", lambda: [])()]
        return calls["movies"][p]
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook, "PROFILES", {})
    # The metadata checks: no TMDB answer, and a last video packet where the header says, as on a healthy file.
    # Language detection is not installed. A test sets calls["tmdb"], "other" or "last_packet", or LID_DIR.
    monkeypatch.setattr(hook.arr_meta, "DOWN", dict(until=0.0, code="", why="", answered=0.0))
    monkeypatch.setattr(hook.arr_meta, "expected_languages", lambda app, ids, token=None, **k: calls.get("tmdb"))
    monkeypatch.setattr(hook.arr_meta, "other_film", lambda *a, **k: calls.get("other"))
    monkeypatch.setattr(hook.arr_meta, "last_packet",
                        lambda p, **k: calls["last_packet"] if "last_packet" in calls else hook.arr_decide.duration(calls["files"].get(p, calls["probe"])))
    settings(lid_dir=str(tmp_path / "no-lid"))
    monkeypatch.setattr(hook, "to_syslog", calls["syslog"].append)
    def fake_arr_write(app, p, method, body=None):   # a command answers completed. calls["on_write"] plays the app's side.
        calls["writes"].append((method, p, body))
        calls.get("on_write", lambda *a: None)(app, p, method, body)
        return {"id": 1, "status": "completed"} if p == "command" else None
    monkeypatch.setattr(hook, "arr_write", fake_arr_write)
    monkeypatch.setattr(hook, "prove", lambda src, tmp, subs, folder, captions=None, **kw: copy.deepcopy(calls["proof"]))   # tests with real files call it
    def fake_mkvmerge(p):   # a file a repack wrote starts with MKV! and reads as calls["mkv_probe"], even after the rename
        mkv = os.path.exists(p) and open(p, "rb").read(4) == b"MKV!"
        if mkv and (calls["files"].get(p, {}).get("container") or {}).get("type") != "Matroska":
            calls["files"][p] = copy.deepcopy(calls["mkv_probe"])
        return copy.deepcopy(calls["files"].setdefault(p, copy.deepcopy(calls["probe"])))

    def fake_window(p, start, secs):   # the video windows, one scripted result per call from calls["window_out"]
        out = calls["window_out"][len(calls["windows"]) % len(calls["window_out"])]
        calls["windows"].append((p, round(start)))
        return dict(out, at=round(start), stopped=out.get("stopped"), took=0.1, read=1000)

    monkeypatch.setattr(hook, "mkvmerge", fake_mkvmerge)
    monkeypatch.setattr(hook, "window", fake_window)
    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    monkeypatch.setattr(hook, "http", fake_http)
    monkeypatch.setattr(hook.os, "fork", fake_fork)
    monkeypatch.setattr(hook.os, "setsid", lambda: None)
    monkeypatch.setattr(hook.os, "dup2", lambda a, b: None)
    monkeypatch.setattr(hook.os, "_exit", fake_exit)
    real_start, real_stop = hook.DEADLINE.start, hook.DEADLINE.stop   # the job's time limit, in events
    monkeypatch.setattr(hook.DEADLINE, "start", lambda secs, why=None: calls["events"].append(f"limit {secs}") or real_start(secs, why))
    monkeypatch.setattr(hook.DEADLINE, "stop", lambda: calls["events"].append("limit off") or real_stop())
    monkeypatch.setattr(hook.fcntl, "flock", fake_flock)
    monkeypatch.setattr(hook.time, "sleep", fake_sleep)
    monkeypatch.setattr(hook.time, "time", lambda: calls["clock"][0])
    monkeypatch.setattr(hook.time, "monotonic", lambda: calls["clock"][0])   # the Plex timers run on it
    for k in list(os.environ):
        if k.startswith(("radarr_", "sonarr_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("radarr_eventtype", "Download")
    monkeypatch.setenv("radarr_movie_id", "7")
    monkeypatch.setenv("radarr_moviefile_path", str(path))
    hook.store.db()
    return calls


def log_lines(env):
    with open(hook.CFG.log) as f:
        return [json.loads(line) for line in f]


def analyzes(env):
    return [urlparse(u).path for m, u, b in env["http"] if m == "PUT"]


def queue(env):
    return hook.queued()


def as_sonarr(monkeypatch, env, series, eps):
    monkeypatch.setattr(hook, "arr", lambda app, p: {"series/5": series, "episode?episodeFileId=9": eps}[p])
    for k in ("radarr_eventtype", "radarr_movie_id", "radarr_moviefile_path"):
        monkeypatch.delenv(k)
    for k, v in (("sonarr_eventtype", "Download"), ("sonarr_series_id", "5"), ("sonarr_episodefile_id", "9"), ("sonarr_episodefile_path", env["path"])):
        monkeypatch.setenv(k, v)



# --- the episode title of an import ------------------------------------------------------------------------------------

SHOW_EPS = [{"id": 31, "seasonNumber": 4, "episodeNumber": 15, "title": "A Rainy Day", "runtime": 11},
            {"id": 32, "seasonNumber": 4, "episodeNumber": 21, "title": "Ship Voyage", "runtime": 11}]


def titled_import(monkeypatch, env, release="Show.D.S04E15.1080p.WEB.H264-GRP", nfo=None):
    """A Sonarr import of S04E15 whose release names its title. nfo writes the release folder Sonarr imported from,
    with a scene NFO that holds nfo. Returns the app's GET calls."""
    as_sonarr(monkeypatch, env, {"id": 5, "title": "Show D", "originalLanguage": {"name": "English"}}, SHOW_EPS[:1])
    calls = []
    def fake_arr(app, p):
        calls.append(p)
        return {"series/5": {"id": 5, "title": "Show D", "originalLanguage": {"name": "English"}}, "episode?episodeFileId=9": SHOW_EPS[:1],
                "episode?seriesId=5": SHOW_EPS}[p]
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setenv("sonarr_episodefile_scenename", release)
    if nfo is not None:
        folder = os.path.join(os.path.dirname(os.path.dirname(env["path"])), "downloads", release)
        os.makedirs(folder)
        open(os.path.join(folder, release.lower() + ".nfo"), "wb").write(nfo.encode("cp437"))
        monkeypatch.setenv("sonarr_episodefile_sourcepath", os.path.join(folder, release.lower() + ".mkv"))
    return calls


@pytest.mark.parametrize("source", ["NFO", "title"])
def test_an_import_whose_release_names_another_episode_alerts_and_changes_nothing(env, monkeypatch, settings, source):
    """The release follows another episode order. Its NFO, or its name, gives the title of what Sonarr lists as S04E21,
    and it was imported as S04E15. The hook alerts the wrong episode, logs the signal, and re-grabs nothing."""
    settings(regrab={"audio", "video", "content"})
    if source == "NFO":
        calls = titled_import(monkeypatch, env, nfo="Title.........: Ship Voyage/That's Not It\nSize....: 1 GB\n")
    else:
        calls = titled_import(monkeypatch, env, release="Show.D.S04E15.Ship.Voyage.1080p.WEB.H264-GRP")
    hook.main([])
    rec = decided(env)
    said, title = ("the release's NFO", "Ship Voyage/That's Not It") if source == "NFO" else ("the release name", "Ship Voyage")
    (sig,) = [x for x in rec["evidence"]["signals"] if x["kind"] == "episode_title"]
    assert sig["why"] == f'imported as S04E15 "A Rainy Day". {said[0].upper()}{said[1:]} calls it "{title}", which is S04E21', sig
    assert {"kind": "episode", "imported": [["S04E15", "A Rainy Day"]], "said": said, "title": title, "names": "S04E21"} in rec["findings"] \
        and sig["verdict"] == "other" and sig["episodes"] == [32] and not rec["evidence"]["regrab"], rec
    assert rec["alert_result"][rec["alert_kinds"].index("episode")] == "sent", rec   # posted to Discord
    assert env["writes"] == [] and calls.count("episode?seriesId=5") == 1, (env["writes"], calls)


def test_an_import_whose_release_names_no_title_reads_no_episode_list(env, monkeypatch):
    calls = titled_import(monkeypatch, env)
    hook.main([])
    rec = decided(env)
    assert "episode?seriesId=5" not in calls and not [x for x in rec["evidence"]["signals"] if x["kind"] == "episode_title"], calls


def test_a_failed_episode_list_leaves_the_other_checks(env, monkeypatch):
    def fake_arr(app, p):
        if p.startswith("episode?seriesId"):
            raise urllib.error.URLError("the app restarts")
        return {"series/5": {"id": 5, "title": "Show D"}, "episode?episodeFileId=9": SHOW_EPS[:1]}[p]
    titled_import(monkeypatch, env, release="Show.D.S04E15.Ship.Voyage.1080p.WEB.H264-GRP")
    monkeypatch.setattr(hook, "arr", fake_arr)
    hook.main([])
    rec = decided(env)
    (sig,) = [x for x in rec["evidence"]["signals"] if x["kind"] == "episode_title"]
    assert sig["verdict"] == "unknown" and "the app restarts" in sig["why"] and "meta_error" not in rec and rec["tmdb"], rec


def test_a_backfill_reads_the_episode_list_once_per_series(env, monkeypatch):
    """The backfill already lists the series' episodes, so the title check of each file reads them from there."""
    eps = [dict(e, episodeFileId=i) for e, i in zip(SHOW_EPS, (21, 22))]
    second = os.path.join(os.path.dirname(env["path"]), "S04E21.mkv")
    shutil.copy(env["path"], second)
    calls = []
    def fake_arr(app, p):
        calls.append(p)
        return {"series": [{"id": 5, "title": "Show D", "statistics": {"episodeFileCount": 2}}], "episode?seriesId=5": eps,
                "episodefile?seriesId=5": [{"id": 21, "path": env["path"], "sceneName": "Show.D.S04E15.Ship.Voyage.1080p.WEB.H264-GRP"},
                                           {"id": 22, "path": second, "sceneName": "Show.D.S04E21.Ship.Voyage.1080p.WEB.H264-GRP"}]}[p]
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "sonarr"])
    verdicts = {r["path"]: [x["verdict"] for x in r["evidence"]["signals"] if x["kind"] == "episode_title"] for r in log_lines(env) if r.get("evidence")}
    assert verdicts == {env["path"]: ["other"], second: ["imported"]} and calls.count("episode?seriesId=5") == 1, (verdicts, calls)


def test_the_nfo_beside_the_source_gives_the_title_only_when_it_is_the_releases_own(tmp_path):
    """A download folder that holds the NFOs of other releases gives no title."""
    rel = tmp_path / "Show.D.S04E15.1080p.WEB.H264-GRP"
    rel.mkdir()
    (rel / "grp.nfo").write_bytes("Title : Ship Voyage\r\n".encode("cp437") + b"\xdb\xdb\r\n")   # cp437 art
    src = str(rel / "show.d.s04e15.1080p.web.h264-grp.mkv")
    assert hook.release_nfo_title(src) == "Ship Voyage"   # the one NFO of a folder named like the release
    (tmp_path / "other.nfo").write_text("Title : Night Shift\n")
    (tmp_path / "another.nfo").write_text("Title : Day One\n")
    assert hook.release_nfo_title(str(tmp_path / "show.d.s04e15.mkv")) is None   # a shared download folder
    (tmp_path / "show.d.s04e15.nfo").write_text("Episode Title : Ship Voyage\n")
    assert hook.release_nfo_title(str(tmp_path / "show.d.s04e15.mkv")) == "Ship Voyage"   # its own name
    loose = tmp_path / "Loose"
    loose.mkdir()
    (loose / "x.nfo").write_text("Title : Night Shift\n")
    assert hook.release_nfo_title(str(loose / "show.d.s04e15.mkv"), "Show.D.S04E15") is None
    assert hook.release_nfo_title(str(tmp_path / "gone" / "x.mkv")) is None and hook.release_nfo_title(None) is None


def test_a_file_with_no_scene_name_says_the_title_comes_from_its_name(env, monkeypatch):
    """Sonarr named the file in the order it had at the import, and the order changed since."""
    renamed = os.path.join(os.path.dirname(env["path"]), "Show D - S04E15 - Ship Voyage WEBDL-1080p.mkv")
    shutil.copy(env["path"], renamed)
    eps = [dict(SHOW_EPS[0], episodeFileId=21), SHOW_EPS[1]]
    monkeypatch.setattr(hook, "arr", lambda app, p: {"series": [{"id": 5, "title": "Show D", "statistics": {"episodeFileCount": 1}}],
                                                      "episode?seriesId=5": eps,
                                                      "episodefile?seriesId=5": [{"id": 21, "path": renamed, "sceneName": None}]}[p])
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "sonarr"])
    (sig,) = [x for r in log_lines(env) if r.get("evidence") for x in r["evidence"]["signals"] if x["kind"] == "episode_title"]
    assert sig["why"] == 'imported as S04E15 "A Rainy Day". The file name calls it "Ship Voyage", which is S04E21', sig


def test_an_nfo_that_is_no_file_never_blocks_the_import(tmp_path):
    """A FIFO named like the video would block open() and the app's import thread with it."""
    os.mkfifo(tmp_path / "show.d.s04e15.nfo")
    got = []
    t = threading.Thread(target=lambda: got.append(hook.release_nfo_title(str(tmp_path / "show.d.s04e15.mkv"))), daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():   # unblock the reader, so the thread ends
        os.close(os.open(tmp_path / "show.d.s04e15.nfo", os.O_WRONLY | os.O_NONBLOCK))
    assert got == [None]


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "episode_title")
TESTER_NFO = "spongebob.squarepants.s04e15.1080p.web.h264-outpost31.nfo"   # as the download client left it, lower case
TESTER_RELEASE = "SpongeBob.SquarePants.S04E15.1080p.WEB.H264-OUTPOST31"   # the folder and the video, mixed case


def tester_import(monkeypatch, env, tmp_path, release=TESTER_RELEASE, nfo=TESTER_NFO):
    """A tester's import of S04E15 from a download folder named like the release, with the NFO nfo in it."""
    eps = json.load(open(os.path.join(FIXTURES, "spongebob-s00-s04-episodes.json")))
    own = [e for e in eps if (e["seasonNumber"], e["episodeNumber"]) == (4, 15)]
    as_sonarr(monkeypatch, env, {}, own)
    monkeypatch.setattr(hook, "arr", lambda app, p: {"series/5": {"id": 5, "title": "SpongeBob SquarePants"}, "episode?episodeFileId=9": own,
                                                      "episode?seriesId=5": eps}[p])
    folder = tmp_path / "downloads" / "complete" / release
    folder.mkdir(parents=True)
    if nfo:
        shutil.copy(os.path.join(FIXTURES, TESTER_NFO), folder / nfo)
    monkeypatch.setenv("sonarr_episodefile_sourcepath", str(folder / f"{release}.mkv"))
    monkeypatch.setenv("sonarr_episodefile_scenename", release)
    return folder


def test_the_testers_release_alerts_the_two_episodes_it_holds(env, monkeypatch, tmp_path):
    """The release follows another episode order. Its NFO names two segments that Sonarr lists as S04E23 and S04E27,
    and Sonarr imported it as S04E15 by its number. The language and the runtime both match."""
    tester_import(monkeypatch, env, tmp_path)
    hook.main([])
    rec = decided(env)
    assert {"kind": "episode", "imported": [["S04E15", "Ghost Host"]], "said": "the release's NFO", "title": "Squidtastic Voyage/That's No Lady",
            "names": "S04E23 and S04E27"} in rec["findings"] and not rec["evidence"]["regrab"] and env["writes"] == [], rec["findings"]
    (e,) = [b["embeds"][0] for m, u, b in env["http"] if m == "POST"]
    assert (e["title"], e["description"]) == ("Maybe the wrong episode", 'Imported as S04E15 **"Ghost Host"**. The release\'s NFO calls it '
                                              '**"Squidtastic Voyage/That\'s No Lady"**, which is S04E23 and S04E27.')


@pytest.mark.parametrize("nfo", [TESTER_NFO, "outpost31.nfo"])
def test_the_testers_nfo_counts_by_its_name_or_as_the_one_nfo_of_the_release_folder(tmp_path, monkeypatch, env, nfo):
    """Its name is the video's in lower case. Renamed, it is still the one NFO of a folder named like the video. With a
    second NFO beside it, only the name decides."""
    folder = tester_import(monkeypatch, env, tmp_path, nfo=nfo)
    source = str(folder / f"{TESTER_RELEASE}.mkv")
    assert hook.release_nfo_title(source, TESTER_RELEASE) == "Squidtastic Voyage/That's No Lady"
    (folder / "sample.nfo").write_text("Title: Ghost Host\n")
    assert hook.release_nfo_title(source, TESTER_RELEASE) == ("Squidtastic Voyage/That's No Lady" if nfo == TESTER_NFO else None)


def test_the_release_now_in_the_library_gives_no_signal(env, monkeypatch, tmp_path):
    """Its title is S04E15's own, so it alerts nothing. The REPACK flag is no part of the title."""
    tester_import(monkeypatch, env, tmp_path, release="SpongeBob.SquarePants.S04E15.Ghost.Host.REPACK.1080p.AMZN.WEB-DL.DDP2.0.H.264-Kitsune",
                  nfo=None)
    hook.main([])
    rec = decided(env)
    (sig,) = [x for x in rec["evidence"]["signals"] if x["kind"] == "episode_title"]
    assert sig["verdict"] == "imported" and "episode" not in rec["alert_kinds"], (sig, rec["findings"])


def test_the_hook_reads_the_nfo_at_the_event(env, monkeypatch):
    """The worker may run after the download client removed the folder, so the queued job holds the title."""
    titled_import(monkeypatch, env, nfo="Title: Ship Voyage\n")
    monkeypatch.setattr(hook.os, "fork", lambda: 4242)
    with pytest.raises(SystemExit):
        hook.main([])
    (name,) = queue(env)
    assert hook.job_of(name)["nfo_title"] == "Ship Voyage"


KODI_NFO = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<episodedetails>\n  <title>A Rainy Day</title>\n</episodedetails>\n'


def episode_signal(rec):
    return [x for x in rec["evidence"]["signals"] if x["kind"] == "episode_title"]


@pytest.mark.parametrize("names", [{".nfo": "Title: Ship Voyage\n"}, {".nfo": KODI_NFO, ".nfo-orig": "Title: Ship Voyage\n"}])
def test_the_worker_reads_the_release_nfo_sonarr_copied_beside_the_video(env, monkeypatch, names):
    """The download folder gave no title. Sonarr copied the release NFO beside the video as <name>.nfo, or renamed it
    <name>.nfo-orig when its Kodi writer wrote <name>.nfo (OtherExtraFileRenamer). The Kodi .nfo is skipped."""
    titled_import(monkeypatch, env)
    for ext, text in names.items():
        open(env["path"][:-4] + ext, "w").write(text)
    hook.main([])
    (sig,) = episode_signal(decided(env))
    assert sig["verdict"] == "other" and sig["why"].endswith('The release\'s NFO calls it "Ship Voyage", which is S04E21'), sig


def test_a_kodi_nfo_alone_gives_no_title(env, monkeypatch):
    titled_import(monkeypatch, env)
    open(env["path"][:-4] + ".nfo", "w").write(KODI_NFO.replace("A Rainy Day", "Ship Voyage"))
    hook.main([])
    assert episode_signal(decided(env)) == [] and hook.library_nfo_title(env["path"]) is None


def test_the_nfo_of_the_download_folder_wins_over_the_one_in_the_library(env, monkeypatch):
    titled_import(monkeypatch, env, nfo="Title: Ship Voyage\n")
    open(env["path"][:-4] + ".nfo", "w").write("Title: A Rainy Day\n")
    hook.main([])
    (sig,) = episode_signal(decided(env))
    assert sig["verdict"] == "other" and "Ship Voyage" in sig["why"], sig


def test_a_backfill_reads_the_release_nfo_in_the_library(env, monkeypatch):
    eps = [dict(SHOW_EPS[0], episodeFileId=21), SHOW_EPS[1]]
    monkeypatch.setattr(hook, "arr", lambda app, p: {"series": [{"id": 5, "title": "Show D", "statistics": {"episodeFileCount": 1}}],
                                                      "episode?seriesId=5": eps,
                                                      "episodefile?seriesId=5": [{"id": 21, "path": env["path"], "sceneName": "Show.D.S04E15.WEB-GRP"}]}[p])
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    open(env["path"][:-4] + ".nfo-orig", "w").write("Episode Title: Ship Voyage\n")
    hook.main(["--backfill", "sonarr"])
    (sig,) = [x for r in log_lines(env) if r.get("evidence") for x in episode_signal(r)]
    assert sig["verdict"] == "other" and sig["why"].endswith('The release\'s NFO calls it "Ship Voyage", which is S04E21'), sig


def test_a_film_reads_no_nfo_beside_it(env, monkeypatch):
    """The title check is for episodes only, so a Radarr import never reads the NFO in the library."""
    open(env["path"][:-4] + ".nfo", "w").write("Title: Ship Voyage\n")
    seen = []
    monkeypatch.setattr(hook, "library_nfo_title", lambda path: seen.append(path))
    hook.main([])
    rec = decided(env)
    assert seen == [] and "evidence" in rec and "meta_error" not in rec, rec.get("meta_error")


def test_a_library_nfo_that_is_no_file_never_blocks(tmp_path):
    """A FIFO named like the video's NFO is never opened, as in release_nfo_title()."""
    os.mkfifo(tmp_path / "show.nfo")
    got = []
    t = threading.Thread(target=lambda: got.append(hook.library_nfo_title(str(tmp_path / "show.mkv"))), daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():   # unblock the reader, so the thread ends
        os.close(os.open(tmp_path / "show.nfo", os.O_WRONLY | os.O_NONBLOCK))
    assert got == [None]


# --- the hook ------------------------------------------------------------------------------------

def test_test_event_answers_at_once(env, monkeypatch, capsys):
    monkeypatch.setenv("radarr_eventtype", "Test")
    env["movies"]["rootfolder"] = [{"path": os.path.dirname(os.path.dirname(env["path"]))}]   # the Test checks the root folders
    monkeypatch.setattr(hook.os, "fork", lambda: pytest.fail("a Test event must not fork"))
    hook.main([])
    assert "Test ok" in capsys.readouterr().out
    assert not os.path.exists(hook.CFG.log)


@pytest.mark.parametrize("event", ["Test", "Grab"])
def test_a_test_event_fails_when_no_policy_loaded(env, monkeypatch, settings, capsys, event):
    """The app's Test fails on the exit code and shows the help of --selftest, as the Docker listener's Test does. Any
    other event gets its answer."""
    monkeypatch.setenv("radarr_eventtype", event)
    monkeypatch.setattr(hook.arr_decide, "POLICY", None)
    monkeypatch.setattr(hook, "POLICY_ERROR", "FileNotFoundError: [Errno 2] No such file or directory")
    settings(policy_file="/nonexistent/policy.json")
    if event == "Grab":
        hook.main([])
        assert capsys.readouterr().out == "arr-media-guard: Grab ok\n"
        return
    with pytest.raises(SystemExit) as ex:
        hook.main([])
    assert str(ex.value) == f"arr-media-guard: {hook.policy_help()}" and "Create it from the example" in str(ex.value)
    assert "Test ok" not in capsys.readouterr().out


def test_parent_queues_the_job_and_returns_before_any_work(env, monkeypatch):
    monkeypatch.setattr(hook.os, "fork", lambda: 4242)
    monkeypatch.setattr(hook, "arr", lambda *a: pytest.fail("the parent must not call the API"))
    with pytest.raises(SystemExit) as ex:
        hook.main([])
    assert ex.value.code == 0 and env["exit"] == [0]
    (name,) = queue(env)
    assert hook.job_of(name)["path"] == env["path"]


def test_download_edits_verifies_logs_and_analyzes(env):
    hook.main([])
    assert env["mkvpropedit"] == [["--edit", "track:=2", "--set", "flag-default=0", "--edit", "track:=3", "--set", "flag-default=1",
                                   "--edit", "track:=4", "--set", "flag-default=0"]]
    editing, rec, plex = log_lines(env)
    assert editing["result"] == "editing" and editing["undo"] and "after" not in editing
    assert rec["outcome"] == "edited" and rec["label"] == "Film A (1979)" and rec["original"] == "English"
    assert rec["undo"] == ["mkvpropedit", env["path"], "--edit", "track:=2", "--set", "flag-default=1", "--edit", "track:=3",
                           "--set", "flag-default=0", "--edit", "track:=4", "--set", "flag-default=1"]
    assert [t["default"] for t in rec["after"] if t["pos"] in ("a1", "a2", "s1")] == [0, 1, 0]
    assert plex["result"] == "plex" and plex["plex"] == "analyze sent for 7101"
    assert analyzes(env) == ["/library/metadata/7101/analyze"] and env["attempts"] == [0]
    assert not [u for m, u, b in env["http"] if "discord" in u or "/refresh" in u]
    assert queue(env) == [] and env["forks"] == 1


def test_second_run_changes_nothing(env):
    hook.main([])
    hook.main([])
    assert len(env["mkvpropedit"]) == 1 and len(analyzes(env)) == 1
    assert [r.get("outcome") or r["result"] for r in log_lines(env)] == ["editing", "edited", "plex", "no_change"]   # a decision line by its outcome


def test_wrong_language_alerts_once(env):
    env["probe"] = copy.deepcopy(NO_ENGLISH)
    hook.main([])
    hook.main([])
    (post,) = [b for m, u, b in env["http"] if m == "POST"]
    host, stamp = hook.CFG.instance, post["embeds"][0]["timestamp"]
    assert post == {"username": f"Radarr {host}", "allowed_mentions": {"parse": []}, "embeds": [{   # the whole payload
        "title": "Wrong audio language", "description": "The audio is **Portuguese**, but it should be English.", "color": hook.COLORS["amber"],
        "fields": [{"name": "Film A (1979)", "value": "Film A (1979) WEBDL-1080p.mkv", "inline": False}],
        "footer": {"text": f"TMDB has no record of this item, so its language check was skipped · arr-media-guard on {host}"},
        "timestamp": stamp}]}, post
    assert stamp.endswith("+00:00")
    assert [u for m, u, b in env["http"] if m == "POST"] == ["https://discord.invalid/ops"]
    assert [r["alert_result"] for r in log_lines(env)] == [["sent"], ["already sent"]]
    assert env["mkvpropedit"] == []


def test_broken_duration_alerts_and_skips_runtime(env, monkeypatch):
    env["probe"]["container"]["properties"]["duration"] = 605 * 10**9
    env["last_packet"] = 7200   # the video runs the full two hours, so no duration is trusted and no runtime verdict exists
    os.truncate(env["path"], int(4.6e9))   # sparse, no disk used
    monkeypatch.setattr(hook, "zero_probe", lambda p, *a: ([], 0, False))   # a sparse file reads as zeros, a real one never is sparse
    hook.main([])
    (rec,) = [r for r in log_lines(env) if r.get("outcome") == "edited"]
    assert rec["alert_kinds"] == ["duration"]


def test_errors_are_logged_and_never_raised(env, monkeypatch):
    def boom(*a):
        raise ConnectionError("Radarr is down")
    monkeypatch.setattr(hook, "arr", boom)
    hook.main([])
    (rec,) = log_lines(env)
    assert rec["result"] == "error: ConnectionError: Radarr is down" and queue(env) == []


def test_non_mkv_is_skipped(env, monkeypatch, settings, tmp_path):
    mp4 = tmp_path / "media" / "Film A (1979)" / "Film A (1979).mp4"
    mp4.write_bytes(b"x")
    monkeypatch.setenv("radarr_moviefile_path", str(mp4))
    settings(convert=False)   # CONVERT=false
    hook.main([])
    assert log_lines(env)[0]["outcome"] == "not_mkv"


def test_stale_jobs_are_dropped(env, monkeypatch):
    monkeypatch.setenv("radarr_moviefile_path", env["path"] + ".gone.mkv")
    hook.main([])
    assert log_lines(env)[-1]["outcome"] == "file_gone"
    monkeypatch.setenv("radarr_moviefile_path", env["path"])
    monkeypatch.setattr(hook.os, "fork", lambda: 4242)
    with pytest.raises(SystemExit):
        hook.main([])       # queued, the worker is not started here
    gc.collect()            # the parent's lock copy closes, as at process exit
    env["clock"][0] += hook.JOB_MAX_AGE + 1
    hook.worker(hook.try_lock("worker.lock"))
    assert log_lines(env)[-1]["outcome"] == "job_stale" and env["mkvpropedit"] == []


def test_sonarr_episode_uses_the_episode_runtime(env, monkeypatch):
    as_sonarr(monkeypatch, env, {"title": "Show D", "tvdbId": 90301, "originalLanguage": {"name": "English"}, "runtime": 62},
              [{"seasonNumber": 2, "episodeNumber": 3, "runtime": 62}])
    env["probe"]["container"]["properties"]["duration"] = 605 * 10**9
    hook.main([])
    (rec,) = [r for r in log_lines(env) if r.get("outcome") == "edited"]
    assert rec["label"] == "Show D S02E03" and rec["app"] == "sonarr"
    assert rec["findings"] == [{"kind": "runtime", "runs": "10:05", "listed": 62}]   # header and last packet agree


def test_reprobe_failure_keeps_the_undo_record(env, monkeypatch):
    probes = iter([copy.deepcopy(PORTUGUESE_DEFAULT)])
    def probe(p):
        try:
            return next(probes)
        except StopIteration:
            raise RuntimeError("mkvmerge: NAS went away")
    monkeypatch.setattr(hook, "mkvmerge", probe)
    hook.main([])
    editing, final = log_lines(env)
    assert editing["result"] == "editing" and editing["undo"][:2] == ["mkvpropedit", env["path"]]
    assert final["result"].startswith("error: RuntimeError")


def test_the_time_limit_is_off_during_mkvpropedit_and_starts_after_the_lock(env):
    hook.main([])
    events = [e for e in env["events"] if e != "status lock"]
    assert events[:6] == ["worker lock", "gate", "lock", f"limit {hook.BUDGET}", "limit off", "mkvpropedit"]
    assert "timeout" not in env["run_kw"][0] and hook.DEADLINE.end is None   # the next job, a deep analysis or a Plex pass has none


def test_a_stuck_lock_holder_times_out_and_is_logged(env, monkeypatch):
    def stuck(f, op):   # each try finds the lock held, and LOCK_WAIT passes
        if f.name.endswith("/lock"):
            env["events"].append("lock")
            assert op & fcntl.LOCK_NB
            env["clock"][0] += hook.LOCK_WAIT
            raise BlockingIOError
        REAL_FLOCK(f, op)
    monkeypatch.setattr(hook.fcntl, "flock", stuck)
    monkeypatch.setattr(hook, "arr", lambda *a: pytest.fail("no work without the lock"))
    hook.main([])
    (rec,) = log_lines(env)
    assert rec["result"] == f"error: OutOfTime: gave up after waiting {hook.LOCK_WAIT} seconds for the lock"
    assert env["mkvpropedit"] == []


@pytest.mark.parametrize("stuck, op, n", [("the exclusive lock", fcntl.LOCK_EX, 1), ("the lock after the hearing", fcntl.LOCK_SH, 2)])
@pytest.mark.parametrize("wait", [hook.LOCK_WAIT - 1, hook.LOCK_WAIT])
def test_a_job_process_waits_for_its_lock_with_a_limit_of_its_own(env, monkeypatch, tmp_path, stuck, op, n, wait):
    """A job process lets the file lock go for the hearing and before the edit, then takes it again. Each wait has
    LOCK_WAIT of its own and costs the job none of its time. A holder that keeps the lock longer ends the job."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Spanish"}
    env["probe"] = copy.deepcopy(BARE_ENG)
    lid(env, monkeypatch, tmp_path, lambda argv: "spa")
    tries = []
    def flock(f, o):   # the n-th try of op finds the lock held for wait seconds
        if f.name.endswith("/lock") and o & fcntl.LOCK_NB:
            tries.append(o & ~fcntl.LOCK_NB)
            if tries[-1] == op and tries.count(op) == n:
                env["clock"][0] += wait
                raise BlockingIOError
        REAL_FLOCK(f, o)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    rec = decided(env)
    if wait < hook.LOCK_WAIT:
        assert rec["outcome"] == "edited" and rec["heard"]["a1"]["lang"] == "spa", rec["result"]
    else:
        assert rec["result"] == f"error: OutOfTime: gave up after waiting {hook.LOCK_WAIT} seconds for {stuck}" and env["mkvpropedit"] == []


def test_a_long_wait_for_the_exclusive_lock_leaves_the_regrab_its_time(env, monkeypatch):
    """The re-grab reads the app's history right after the exclusive wait, under the job's time limit. The wait cost
    the job none of its time."""
    env["ffmpeg_out"] = [SILENCE]
    env["movies"]["history?downloadId=a1b2c3d4&pageSize=1000"] = GRAB
    real = hook.arr
    monkeypatch.setattr(hook, "arr", lambda app, p: hook.DEADLINE.check() or real(app, p))   # as http() does
    def flock(f, o):   # the first exclusive try finds the lock held for an hour less a second
        if f.name.endswith("/lock") and o == fcntl.LOCK_EX | fcntl.LOCK_NB and not env.get("waited"):
            env["waited"] = True
            env["clock"][0] += hook.LOCK_WAIT - 1
            raise BlockingIOError
        REAL_FLOCK(f, o)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    hook.run_job(enqueue(env, 0, env["path"], file_id="11", download_id="a1b2c3d4"), [], shared=True)
    rec = decided(env)
    assert (rec["outcome"], rec["audio"]["certain"]) == ("broken_audio", "the audio is silent at all 3 places checked") and env["waited"]
    assert env["writes"][0] == ("DELETE", "moviefile/11", None)


def test_a_younger_job_that_waited_out_its_turn_gives_up_before_its_edit(env, monkeypatch):
    """wait_turn() returns once LOCK_WAIT passed, with an older job of the download still unsettled. The exclusive step
    then gives up with its own text, and the younger job edits nothing."""
    monkeypatch.setattr(hook, "wait_turn", lambda name, job: env["clock"].__setitem__(0, env["clock"][0] + hook.LOCK_WAIT))
    hook.run_job(enqueue(env, 0, env["path"], download_id="a1b2c3d4"), [], shared=True)
    rec = decided(env)
    assert rec["result"] == f"error: OutOfTime: gave up after waiting {hook.LOCK_WAIT} seconds for the exclusive lock" and env["mkvpropedit"] == []


def test_a_job_whose_limit_a_handler_took_has_one_again_after_its_exclusive_wait(env, monkeypatch):
    """The job's time runs out in the video check, which takes the OutOfTime, so the limit ends. exclusive() gives the
    job BUDGET again."""
    real, after = hook.exclusive, []
    monkeypatch.setattr(hook, "exclusive", lambda *a: real(*a) or after.append(hook.DEADLINE.left()))
    def zero_probe(path, again=False, stop=lambda: False):
        env["clock"][0] = hook.DEADLINE.end
        return hook.DEADLINE.check()
    monkeypatch.setattr(hook, "zero_probe", zero_probe)
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    rec = decided(env)
    assert rec["video"]["code"] == "video_check_error" and after == [hook.BUDGET] and rec["outcome"] == "edited", (rec["result"], after)


def test_a_job_that_fails_leaves_no_time_limit(env, monkeypatch):
    """A deep analysis or a Plex pass after the job runs in the same process, and has no time limit."""
    monkeypatch.setattr(hook, "check_audio", lambda *a, **k: 1 / 0)
    hook.main([])
    assert log_lines(env)[-1]["result"] == "error: ZeroDivisionError: division by zero" and hook.DEADLINE.end is None


def test_a_long_remux_costs_the_checks_after_it_none_of_their_time(env, monkeypatch):
    """The remux and its proof run with no time limit, and the checks after a repack get a new BUDGET. A repack of a
    large file on a slow NAS takes far longer than BUDGET."""
    mp4_named_mkv(env)
    env["during_repack"] = lambda: env["clock"].__setitem__(0, env["clock"][0] + 10 * hook.BUDGET)
    fake, remux = hook.subprocess.run, []
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: ("-o" in argv and remux.append(kw)) or fake(argv, **kw))
    hook.main([])
    rec = decided(env)
    assert (rec["result"], rec["reasons"][0], len(env["repacks"])) == ("edited", "repacked", 1), rec["result"]
    assert remux and all("timeout" not in kw for kw in remux)


def test_a_subprocess_takes_its_timeout_from_the_time_left(monkeypatch):
    """sample() gives ffmpeg 120 s, cut to the job's time left. A cut raises OutOfTime. A timeout of its own is a slow
    read, so the sample did not run and the job goes on."""
    clock, asked = [1000.0], []
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    def run(argv, timeout, **kw):   # ffmpeg hangs until its timeout
        asked.append(timeout)
        clock[0] += timeout
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(hook.subprocess, "run", run)
    hook.DEADLINE.start(30)
    with pytest.raises(hook.arr_meta.OutOfTime, match="^stopped after 30 seconds$"):
        hook.sample("x.mkv", 0, 0)
    hook.DEADLINE.start(hook.BUDGET)
    assert hook.sample("x.mkv", 0, 0)["ran"] is False and hook.DEADLINE.left() == hook.BUDGET - 120
    hook.DEADLINE.stop()
    assert hook.sample("x.mkv", 0, 0)["ran"] is False and asked == [30, 120, 120]


def test_an_http_call_takes_its_timeout_from_the_time_left(monkeypatch):
    """http() gives each socket wait its timeout, cut to the job's time left. A call that the cut ends, or one that
    answers past the time left, raises OutOfTime. A network handler would swallow the TimeoutError."""
    clock, asked, took = [1000.0], [], [1.0]   # took: seconds of each of the two socket waits, the answer and the body
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    class Answer:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            clock[0] += took[0]
            return b'{"ok": 1}'
    def urlopen(req, timeout):
        asked.append(timeout)
        if took[0] >= timeout:
            clock[0] += timeout
            raise TimeoutError("timed out")
        clock[0] += took[0]
        return Answer()
    monkeypatch.setattr(hook.urllib.request, "urlopen", urlopen)
    assert hook.http("http://app.invalid/api") == {"ok": 1} and asked == [15]   # no limit
    hook.DEADLINE.start(5)
    assert hook.http("http://app.invalid/api") == {"ok": 1} and asked[-1] == 5
    took[0] = 2.0   # 3 s left: each wait ends in time, and the answer comes after the time left
    with pytest.raises(hook.arr_meta.OutOfTime, match="^stopped after 5 seconds$"):
        hook.http("http://app.invalid/api")
    took[0] = 10.0   # the server hangs
    hook.DEADLINE.start(5)
    with pytest.raises(hook.arr_meta.OutOfTime):
        hook.http("http://app.invalid/api")
    assert asked == [15, 5, 3, 5]


def kept_records_taken():
    with hook.kept_records():
        pass


@contextlib.contextmanager
def status_held():
    """Another process that holds the lock of status.json."""
    with open(os.path.join(hook.CFG.state_dir, "status.json.lock"), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def store_held():
    """Another process in a write transaction of the state store."""
    hook.store.db()
    with contextlib.closing(sqlite3.connect(hook.store.path(), isolation_level=None)) as other:
        other.execute("BEGIN IMMEDIATE")
        yield
        other.execute("ROLLBACK")


@pytest.mark.parametrize("held, take, done", [
    (status_held, lambda: hook.status("tmdb", "found"), lambda: os.path.exists(os.path.join(hook.CFG.state_dir, "status.json"))),
    (store_held, lambda: hook.remember_folder("/nonexistent/.keep"), lambda: hook.kept_folders() == {"/nonexistent/.keep"}),
    (store_held, kept_records_taken, lambda: hook.store.get("kept", "replaced") == [])])
def test_a_stuck_state_lock_ends_at_the_time_limit(held, take, done):
    """The metadata checks and a conversion's subtitle check take the status lock and write the store under the job's
    time limit. Another process that holds one ends the job."""
    with held():
        hook.DEADLINE.start(0.3)
        started = time.perf_counter()
        with pytest.raises(hook.arr_meta.OutOfTime):
            take()
        assert time.perf_counter() - started < 5 and not done()   # the wait ends at the limit, never after a wait of its own
    take()   # free, and no limit
    assert done()


@pytest.mark.parametrize("read", ["mkvmerge", "ffprobe_audio", "ffprobe_duration", "video_stream_seconds", "ff_streams", "convert_captions"])
def test_each_read_under_the_time_limit_ends_at_it(monkeypatch, tmp_path, read):
    clock, asked = [1000.0], []
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    def run(argv, timeout, **kw):   # the tool hangs until its timeout
        asked.append(timeout)
        clock[0] += timeout
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(hook.subprocess, "run", run)
    hook.DEADLINE.start(30)
    with pytest.raises(hook.arr_meta.OutOfTime):
        getattr(hook, read)(*(("x.mkv", [{"index": 3}], str(tmp_path), 0) if read == "convert_captions" else ("x.mkv",)))
    assert asked == [30]


def test_a_discord_429_wait_ends_at_the_time_limit(monkeypatch, settings):
    clock, sleeps, tries = [1000.0], [], []
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hook.time, "sleep", lambda s: sleeps.append(s) or clock.__setitem__(0, clock[0] + s))
    def urlopen(req, timeout):
        tries.append(timeout)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"retry_after": 20}'))
    monkeypatch.setattr(hook.urllib.request, "urlopen", urlopen)
    settings(discord_webhook="https://discord.invalid/ops")
    hook.DEADLINE.start(5)
    assert hook.post("radarr", {"title": "x"}) == "failed: OutOfTime: stopped after 5 seconds" and sleeps == [5] and tries == [5]


def test_the_checks_after_a_conversion_have_a_time_limit_of_their_own(env, monkeypatch):
    """The remux ran with no time limit, and the checks after it get a new BUDGET. Audio samples that hang past it end
    the job, as before the conversion."""
    mp4_named_mkv(env)
    fake = hook.subprocess.run
    def run(argv, **kw):
        if argv[0] == "ffmpeg" and "volumedetect" in argv:   # an audio sample hangs until its timeout
            env["clock"][0] += kw["timeout"]
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return fake(argv, **kw)
    monkeypatch.setattr(hook.subprocess, "run", run)
    hook.main([])
    results = [r["result"] for r in log_lines(env)]
    assert results[0] == "repacked" and results[-1] == f"error: OutOfTime: stopped after {hook.BUDGET} seconds", results


def test_discord_429_waits_and_retries_once(env, monkeypatch):
    env["probe"] = copy.deepcopy(NO_ENGLISH)
    tries = []
    def fake_http(url, method="GET", body=None, headers=None, timeout=15):
        tries.append(url)
        if len(tries) == 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b'{"retry_after": 0.4}'))
        return b""
    monkeypatch.setattr(hook, "http", fake_http)
    hook.main([])
    assert env["sleeps"] == [0.4] and len(tries) == 2
    assert log_lines(env)[0]["alert_result"] == ["sent"]


def test_secrets_never_reach_the_log(env, monkeypatch):
    def boom(*a):
        raise ValueError("bad URL https://plex.invalid:32400/?X-Plex-Token=plex-t0ken-1234 and https://discord.invalid/ops")
    monkeypatch.setattr(hook, "arr", boom)
    hook.main([])
    line = open(hook.CFG.log).read()
    assert "plex-t0ken-1234" not in line and "discord.invalid" not in line and "<PLEX_TOKEN>" in line


def test_a_long_webhook_password_never_reaches_the_log(env, settings):
    settings(webhook_password="Kx9-generated-0123456789-abcdefghij")
    hook.log(dict(source="test", result="warning", note="the body named Kx9-generated-0123456789-abcdefghij"))
    assert log_lines(env)[-1]["note"] == "the body named <WEBHOOK_PASSWORD>"


def test_hardlinked_file_is_not_edited(env, tmp_path):
    os.link(env["path"], tmp_path / "client-copy.mkv")
    hook.main([])
    assert env["mkvpropedit"] == [] and log_lines(env)[0]["outcome"] == "hardlinked"


@pytest.mark.parametrize("client", [False, True])
def test_the_hooks_own_grab_link_never_blocks_an_edit(env, monkeypatch, settings, tmp_path, client):
    """A grab linked the file, with KEEP_REPLACED on. The edit reaches the kept copy too, which is right. A download
    client's link still blocks it."""
    settings(keep_replaced=True, keep_days=7)
    monkeypatch.setattr(hook, "mount_top", lambda f: str(tmp_path / "media"))
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.keep_grab("radarr", 7, "d1")
    if client:
        os.link(env["path"], tmp_path / "client-copy.mkv")
    hook.main([])
    assert len(env["mkvpropedit"]) == (0 if client else 1)
    assert [r["outcome"] for r in log_lines(env) if r.get("outcome")] == ["hardlinked" if client else "edited"]


def test_sonarr_alerts_go_to_the_same_webhook_as_sonarr(env, monkeypatch):
    as_sonarr(monkeypatch, env, {"title": "Show", "originalLanguage": {"name": "English"}}, [{"seasonNumber": 1, "episodeNumber": 2, "runtime": 44}])
    env["probe"] = copy.deepcopy(NO_ENGLISH)
    hook.main([])
    (post,) = [b for m, u, b in env["http"] if m == "POST"]   # the same webhook, posted as Sonarr
    assert [u for m, u, b in env["http"] if m == "POST"] == ["https://discord.invalid/ops"]
    assert post["username"] == f"Sonarr {hook.CFG.instance}" and post["embeds"][0]["fields"][0]["name"] == "Show S01E02"


# --- the queue and its one worker ----------------------------------------------------------------

def test_200_events_start_at_most_one_worker(env):
    running = hook.try_lock("worker.lock")   # a worker is busy
    for _ in range(200):
        hook.main([])
    assert env["forks"] == 0 and len(queue(env)) == 200
    running.close()                          # it finishes, the next event starts the only new one
    hook.main([])
    assert env["forks"] == 1 and queue(env) == []
    results = [r["result"] for r in log_lines(env)]
    assert results.count("edited") == 1 and results.count("no change") == 200 and results.count("plex") == 1


def test_a_job_queued_while_the_worker_releases_is_processed(env, monkeypatch):
    real = hook.queued
    state = {"first": True}
    def queued():
        jobs = real()
        if state["first"]:        # the worker saw an empty queue. Now an event queues a job and finds the lock held.
            state["first"] = False
            hook.hook()
        return jobs
    monkeypatch.setattr(hook, "queued", queued)
    hook.worker(hook.try_lock("worker.lock"))
    assert env["forks"] == 0 and real() == []
    assert [r.get("outcome") or r["result"] for r in log_lines(env)] == ["editing", "edited", "plex"]   # a decision line by its outcome


def test_the_next_event_runs_a_crashed_workers_job(env, monkeypatch):
    real_item = hook.Radarr.item
    state = {"n": 0}
    def item(*a):
        state["n"] += 1
        if state["n"] == 1:
            raise SystemExit("killed")
        return real_item(*a)
    monkeypatch.setattr(hook.Radarr, "item", item)
    with pytest.raises(SystemExit):
        hook.main([])
    gc.collect()                     # the dead worker's lock file closes, as at process exit
    assert len(queue(env)) == 1 and not os.path.exists(hook.CFG.log)
    hook.main([])                    # a new event starts a new worker, which takes the old job first
    assert queue(env) == [] and env["forks"] == 2
    assert [r.get("outcome") or r["result"] for r in log_lines(env)] == ["editing", "edited", "no_change", "plex"]   # the analyze waits PLEX_QUIET


def test_plex_waits_interleave_with_file_jobs(env, tmp_path):
    second = tmp_path / "media" / "Film A (1979)" / "Second.mkv"
    second.write_bytes(b"x")
    env["movies"]["movie/8"] = dict(env["movies"]["movie/7"], tmdbId=90002, imdbId=None)
    env["plex_items"].append(plex_item("7102", "tmdb://90002", str(second)))
    env["plex_misses"] = 2
    running = hook.try_lock("worker.lock")
    hook.main([])
    os.environ["radarr_movie_id"], os.environ["radarr_moviefile_path"] = "8", str(second)
    hook.main([])
    running.close()
    hook.worker(hook.try_lock("worker.lock"))
    order = [(r["result"], os.path.basename(r["path"])) for r in log_lines(env)]
    assert order == [("editing", "Film A (1979) WEBDL-1080p.mkv"), ("edited", "Film A (1979) WEBDL-1080p.mkv"),
                     ("editing", "Second.mkv"), ("edited", "Second.mkv"),
                     ("plex", "Film A (1979) WEBDL-1080p.mkv"), ("plex", "Second.mkv")]
    assert env["attempts"] == [0, 0, 15, 15]   # the first file's wait did not hold up the second file
    assert env["checks"] == [15, 30, 30]   # one shared read serves both first checks, then a fresh read before each PUT
    assert analyzes(env) == ["/library/metadata/7101/analyze", "/library/metadata/7102/analyze"]


# --- Plex --------------------------------------------------------------------------------------

def test_plex_item_found_after_retries(env):
    env["plex_misses"] = 3
    hook.main([])
    assert env["attempts"] == [0, 15, 45, 105] and analyzes(env) == ["/library/metadata/7101/analyze"]
    assert log_lines(env)[-1]["plex"] == "analyze sent for 7101"


def test_plex_item_never_found_gives_up_after_ten_minutes(env):
    env["plex_misses"] = 99
    hook.main([])
    assert env["attempts"] == [0, 15, 45, 105, 225, 405, 600] and analyzes(env) == []
    assert log_lines(env)[-1]["plex"] == "not in Plex after 600 seconds, no analyze."


def test_plex_path_mismatch_is_never_analyzed(env):
    env["plex_items"][0]["Media"][0]["Part"][0]["file"] = "/data/movies/Film A (1979)/old copy.mkv"
    hook.main([])
    assert analyzes(env) == []
    assert log_lines(env)[-1]["plex"] == "Plex has the item but not this file after 600 seconds, no analyze."


def test_plex_matches_any_id_and_falls_back_to_the_path(env):
    env["plex_items"] = [plex_item("7101", "imdb://tt9000001", env["path"])]   # no tmdb guid in Plex
    hook.main([])
    assert analyzes(env) == ["/library/metadata/7101/analyze"]
    env["plex_items"] = [plex_item("8101", "tmdb://90099", env["path"])]       # Plex matched other metadata
    env["probe"] = copy.deepcopy(PORTUGUESE_DEFAULT); env["files"].clear()
    hook.main([])
    assert analyzes(env)[-1] == "/library/metadata/8101/analyze"


def test_plex_wait_happens_after_the_file_lock_is_released(env, monkeypatch):
    class Lock:   # process() unlocks the real file after the edit, so the fake wraps one
        f = open(os.path.join(hook.CFG.state_dir, "lock"), "w")
        name = f.name
        def fileno(self):
            return self.f.fileno()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.f.close()
            env["events"].append("unlock")
    monkeypatch.setattr(hook, "locked", lambda shared=False, wait=None: env["events"].append("lock") or Lock())
    hook.main([])
    assert env["events"].index("unlock") < env["events"].index("plex")


def test_sonarr_episode_found_by_path_across_numbering(env, monkeypatch):
    # Sonarr S02E09 is Plex S03E04: the path decides, the numbers do not
    show = {"ratingKey": "50", "Guid": [{"id": "tvdb://90201"}]}
    leaves = [{"ratingKey": "6101", "parentIndex": 2, "index": 9, "Media": [{"Part": [{"file": "/elsewhere/e.mkv"}]}]},
              {"ratingKey": "6102", "parentIndex": 3, "index": 4, "Media": [{"Part": [{"file": env["path"]}]}]}]
    real = hook.http
    def plex(url, method="GET", **k):
        u = urlparse(url)
        if u.path == "/library/sections/12/all": return {"MediaContainer": {"Metadata": [show]}}
        if u.path == "/library/metadata/50/allLeaves": return {"MediaContainer": {"Metadata": leaves}}
        return real(url, method, **k)
    monkeypatch.setattr(hook, "http", plex)
    as_sonarr(monkeypatch, env, {"title": "Show E", "tvdbId": 90201, "tmdbId": 90202, "originalLanguage": {"name": "English"}},
              [{"seasonNumber": 2, "episodeNumber": 9, "runtime": 11}])
    hook.main([])
    assert analyzes(env) == ["/library/metadata/6102/analyze"]


# --- no analyze while Plex scans the item's section (Plex can crash on that race) ---

def scan(section="12"):
    ctx = {"Context": {"librarySectionID": section}} if section else {}   # a scan names its section about a second in
    return {"type": "library.update.section", "title": "Scanning Movies", "subtitle": "Film A", **ctx}


def plex_lines(env):
    return [(r["result"], r["plex_reason"]) for r in log_lines(env) if r["result"].startswith("plex")]


def test_analyze_waits_for_two_idle_checks(env):
    env["activities"] = [[{"type": "media.generate.intros", "title": "Detecting intros"}, scan("7")]]   # other work, another section
    hook.main([])
    assert env["checks"] == [0, 15] and analyzes(env) == ["/library/metadata/7101/analyze"]
    last = [(m, urlparse(u).path) for m, u, b in env["http"] if "plex.invalid" in u][-2:]
    assert last == [("GET", "/activities"), ("PUT", "/library/metadata/7101/analyze")]   # the PUT follows an idle check
    assert plex_lines(env) == [("plex", "plex_analyze_sent")] and log_lines(env)[-1]["section"] == "12"


def test_a_busy_section_defers_the_analyze_until_it_is_idle_twice(env):
    env["activities"] = [[scan()], [], [scan()]]   # busy, idle, a scan starts before the second check, then idle
    hook.main([])
    assert env["checks"] == [0, 30, 45, 75, 90] and len(analyzes(env)) == 1
    assert plex_lines(env) == [("plex_deferred", "plex_section_busy"), ("plex", "plex_analyze_sent")]   # one line per state change
    assert log_lines(env)[-2]["plex"] == "Scanning Movies Film A" and log_lines(env)[-1]["deferrals"] == 2


def test_a_section_busy_past_the_cap_skips_the_analyze(env):
    env["activities"] = [[scan()]] * 100
    hook.main([])
    assert env["checks"] == list(range(0, hook.PLEX_BUSY_CAP + 1, hook.PLEX_BUSY_WAIT)) and analyzes(env) == []
    assert plex_lines(env) == [("plex_deferred", "plex_section_busy"), ("plex", "plex_analyze_skipped_busy")]
    assert "No analyze" in log_lines(env)[-1]["plex"] and log_lines(env)[-1]["deferrals"] == 60 and queue(env) == []


def test_an_unknown_section_or_an_error_is_never_idle(env):
    env["activities"] = [[dict(scan(None), Context={"librarySectionID": None})], urllib.error.URLError("refused ?X-Plex-Token=plex-t0ken-1234")]
    hook.main([])
    assert env["checks"] == [0, 30, 60, 75] and len(analyzes(env)) == 1
    assert plex_lines(env) == [("plex_deferred", "plex_section_busy"), ("plex_deferred", "plex_check_failed"), ("plex", "plex_analyze_sent")]
    assert "plex-t0ken-1234" not in open(hook.CFG.log).read()
    with pytest.raises(LookupError):
        hook.plex_scan(None, [])
    assert hook.plex_scan("12", [scan(None), scan("")]) and hook.plex_scan("12", [scan()]) and not hook.plex_scan("12", [scan("7")])


def test_a_single_item_refresh_is_busy_for_its_section_or_every_section(env):
    # the shape Plex sends. The Context names only the item, never a section.
    refresh = {"uuid": "00000000-0000-4000-8000-000000000001", "type": "library.refresh.items", "cancellable": False, "userID": 1,
               "title": "Refreshing", "subtitle": "Checking files", "progress": 0, "Context": {"key": "/library/metadata/8201"}}
    in_7 = dict(refresh, Context={"key": "/library/metadata/8201", "librarySectionID": "7"})
    env["activities"] = [[refresh], [in_7]]   # busy for section 12, then a refresh scoped to another section
    hook.main([])
    assert env["checks"] == [0, 30, 45] and len(analyzes(env)) == 1
    assert plex_lines(env) == [("plex_deferred", "plex_section_busy"), ("plex", "plex_analyze_sent")]
    assert log_lines(env)[-2]["plex"] == "Refreshing Checking files"


def test_the_check_before_a_put_is_a_fresh_read(env, monkeypatch):
    # One worker pass over B1 (ready), A (an 8 s lookup, during which a section 12 scan starts)
    # and B2 (ready). With the pass's shared read, B2's analyze went out while the scan ran.
    live, puts = {"acts": []}, []
    monkeypatch.setattr(hook, "plex_get", lambda path, **q: {"Activity": list(live["acts"])})
    def find(path, want):
        env["clock"][0] += 8
        live["acts"] = [scan()]
        return [], False, None
    monkeypatch.setattr(hook, "plex_find", find)
    monkeypatch.setattr(hook, "http", lambda url, method="GET", **k: puts.append((urlparse(url).path, bool(live["acts"]))))
    def ready(key):   # keys known, first idle check 20 s ago
        p = hook.plex_job("radarr", "hook", key, "/m/" + key, {}, None)
        p.update(keys=[key], section="12", since=env["clock"][0] - 20, quiet=env["clock"][0] - 20)
        return p
    cache = {}
    steps = [hook.plex_step(p, cache=cache) for p in (ready("B1"), hook.plex_job("radarr", "hook", "A", "/m/A", {}, None), ready("B2"))]
    assert puts == [("/library/metadata/B1/analyze", False)]
    assert steps == [("analyze sent for B1", "plex_analyze_sent"), (None, None), (None, "plex_section_busy")]


@pytest.fixture(autouse=True)
def plex_analyzed():
    """The worker's record of the last analyze per section starts empty in each test. The store's record is per STATE_DIR."""
    hook.PLEX_ANALYZED.clear()


@pytest.fixture(autouse=True)
def state_dir_in_tmp(tmp_path_factory, monkeypatch, settings):
    """STATE_DIR is a folder of the test, so no test writes to the host's. env sets its own. hook() bounds the waits for
    the store of its process, and each test starts with the usual ones."""
    settings(state_dir=str(tmp_path_factory.mktemp("state")))
    monkeypatch.setattr(hook.store, "wait", hook.store.WAIT)
    monkeypatch.setattr(hook.store, "until", None)


def drain(pending):
    """The worker's Plex loop: a pass, then a sleep until the next item is due. The fake clock moves with the sleep."""
    while pending:
        hook.plex_pass(pending)
        if pending:
            hook.time.sleep(max(0, min(p["due"] for p in pending) - hook.time.monotonic()))


def refreshes(env, monkeypatch):
    """Let the fake Plex take a partial scan, and record it as (path, query)."""
    real, sent = hook.http, []
    def http(url, method="GET", body=None, **k):
        u = urlparse(url)
        if u.path.endswith("/refresh"):
            env["events"].append("scan")
            sent.append((method, u.path, parse_qs(u.query)))
            return b""
        return real(url, method, body, **k)
    monkeypatch.setattr(hook, "http", http)
    return sent


def test_a_folder_scan_waits_for_two_idle_checks_and_scans_only_that_folder(env, monkeypatch):
    """A restored or repacked file under another name is not in Plex until Plex scans its folder. The scan goes out after
    the same two idle checks of the section as an analyze, for that one folder, and never as an analyze."""
    sent = refreshes(env, monkeypatch)
    folder = os.path.dirname(env["path"])
    env["activities"] = [[scan()], []]   # busy, then idle twice
    pending = [hook.plex_folder_job("radarr", "hook", "Film A (1979)", folder, "abc123")]
    drain(pending)
    assert sent == [("GET", "/library/sections/12/refresh", {"path": [folder], "X-Plex-Token": ["plex-t0ken-1234"]})]
    assert env["checks"] == [0, 30, 45] and analyzes(env) == []
    assert plex_lines(env) == [("plex_deferred", "plex_section_busy"), ("plex", "plex_scan_sent")]
    assert log_lines(env)[-1]["plex"] == f"scan sent for {folder}" and log_lines(env)[-1]["decision_id"] == "abc123"
    assert hook.plex_folder_scan({"path": "/elsewhere/Movie"}) == ([], False, None)   # no section holds it: no scan


def test_a_folder_scan_and_an_analyze_of_one_section_never_go_out_together(env, monkeypatch):
    """Plex can crash when an analyze meets a scan of its section. Plex's work after an analyze is no
    activity the gate sees, so the scan goes out first, even when the analyze was queued first. The analyze then starts
    its two idle checks again, finds the scan busy, and goes out once the section is idle twice."""
    sent = refreshes(env, monkeypatch)
    now = hook.time.monotonic()
    ready = [hook.plex_job("radarr", "hook", "A", env["path"], {"guids": []}, None),
             hook.plex_folder_job("radarr", "hook", "B", os.path.dirname(env["path"]), None)]
    for p in ready:
        p.update(keys=["7101"] if p["label"] == "A" else ["folder"], section="12", since=now - 20, quiet=now - 20)
    env["activities"] = [[], [], [scan()], [scan()]]   # A's held check, B's check, then the scan B started runs 45 s
    order = []
    monkeypatch.setattr(hook, "plex_note", lambda p, done, code: order.append((p["label"], code, hook.time.monotonic() - now)))
    drain(ready)
    assert len(sent) == 1 and len(analyzes(env)) == 1
    assert order == [("B", "plex_scan_sent", 0), ("A", "plex_section_busy", 15), ("A", "plex_section_busy", 45), ("A", "plex_analyze_sent", 90)]
    assert env["checks"] == [0, 0, 15, 45, 75, 90]   # A held, B sends, A busy twice, then idle twice 15 s apart


def test_a_scan_queued_after_an_analyze_waits_for_two_idle_checks(env, monkeypatch):
    """An analyze that went out before the scan was queued is no longer pending, so nothing held the scan.
    Plex's work after an analyze is no activity the gate sees. So the scan waits PLEX_SCAN_AFTER seconds after
    the section's last analyze, then two idle checks. A scan that failed may still have reached Plex, so it restarts the
    checks of the section's analyzes like a scan that went out."""
    sent = refreshes(env, monkeypatch)
    now = hook.time.monotonic()
    a = hook.plex_job("radarr", "hook", "A", env["path"], {"guids": []}, None)
    a.update(keys=["7101"], section="12", since=now - 20, quiet=now - 20)
    pending = [a]
    hook.plex_pass(pending)   # A goes out alone
    b = hook.plex_folder_job("radarr", "hook", "B", os.path.dirname(env["path"]), None)
    b.update(keys=["folder"], section="12")
    pending.append(b)
    order = []
    real_note = hook.plex_note
    monkeypatch.setattr(hook, "plex_note", lambda p, done, code: (order.append((p["label"], code, hook.time.monotonic() - now)), real_note(p, done, code))[1])
    hook.plex_pass(pending)
    assert pending == [b] and b["quiet"] is None and b["due"] == now + hook.PLEX_SCAN_AFTER and sent == []   # held, its lookup done
    drain(pending)
    assert len(sent) == 1 and env["checks"] == [0, 300, 315]   # A's check, then B's two idle checks after the wait
    assert order == [("B", "plex_scan_after_analyze", 0), ("B", "plex_scan_sent", 315)]
    assert plex_lines(env)[-2:] == [("plex_deferred", "plex_scan_after_analyze"), ("plex", "plex_scan_sent")]
    hook.PLEX_ANALYZED.clear()
    assert not hook.plex_scan_first(b, [b, a]) and hook.plex_scan_first(a, [a, b]) and not hook.plex_scan_first(dict(a, section=None), [a, b])
    plex = hook.http
    def refused(url, method="GET", body=None, **k):   # the scan request fails, the idle checks answer
        if "/refresh" in url:
            raise urllib.error.URLError("refused")
        return plex(url, method, body, **k)
    monkeypatch.setattr(hook, "http", refused)
    c = hook.plex_folder_job("radarr", "hook", "C", os.path.dirname(env["path"]), None)
    a.update(quiet=hook.time.monotonic() - 20)
    c.update(keys=["folder"], section="12", since=a["quiet"], quiet=a["quiet"])
    pending = [c, a]
    hook.plex_pass(pending)
    assert pending == [a] and a["quiet"] == hook.time.monotonic() and plex_lines(env)[-1] == ("plex", "plex_scan_failed")   # its first new check


def test_a_worker_folder_scan_waits_for_a_backfill_analyze_in_the_log(env, monkeypatch):
    """A backfill is another process, so the worker's own record of its analyzes misses it. The decision log has it. The
    scan waits PLEX_SCAN_AFTER after it, then takes two new idle checks, as --plex-flush does."""
    sent = refreshes(env, monkeypatch)
    at = []
    real_http = hook.http
    monkeypatch.setattr(hook, "http", lambda url, *a, **k: (at.append(hook.time.time()) if "/refresh" in url else None) or real_http(url, *a, **k))
    start = hook.time.time()
    log_at_clock(dict(app="radarr", source="backfill", result="edited", plex="analyze sent for 7101", plex_reason="plex_analyze_sent",
                      plex_section="12"))
    drain([hook.plex_folder_job("radarr", "hook", "Film A (1979)", os.path.dirname(env["path"]), "abc123")])
    assert len(sent) == 1 and at[0] - start >= hook.PLEX_SCAN_AFTER
    c = env["checks"]   # two idle checks, the wait for the logged analyze, two new checks. The log's time has whole seconds.
    assert len(c) == 4 and c[:2] == [0, 15] and hook.PLEX_SCAN_AFTER - 1 < c[2] <= hook.PLEX_SCAN_AFTER and c[3] - c[2] == 15
    assert plex_lines(env) == [("plex_deferred", "plex_scan_after_analyze"), ("plex", "plex_scan_sent")]


ANALYZE_LINE = dict(app="radarr", source="hook", result="plex", plex="analyze sent for 7101", plex_reason="plex_analyze_sent",
                    section="12")


def holds(section="12"):
    """Whether a scan of section waits for the whole PLEX_SCAN_AFTER. The log's time has whole seconds."""
    return hook.after_analyze(section) > hook.PLEX_SCAN_AFTER - 1


def test_an_analyze_in_the_log_holds_a_scan_after_the_log_rotates(env):
    """log() keeps the time of each analyze line in the store, so a rotation or a truncation of the log loses none. A
    scan or another section's analyze holds nothing."""
    hook.log(dict(ANALYZE_LINE, plex_reason="plex_scan_sent"))
    hook.log(dict(ANALYZE_LINE, section="13"))
    assert hook.after_analyze("12") == 0
    hook.log(ANALYZE_LINE)
    os.rename(hook.CFG.log, hook.CFG.log + ".1")
    open(hook.CFG.log, "w").close()
    assert holds() and holds("13") and hook.after_analyze("14") == 0


@pytest.mark.parametrize("line, section", [
    (dict(ANALYZE_LINE, plex_reason="plex_analyze_failed"), "12"),   # a failed request may still have reached Plex
    ({k: v for k, v in ANALYZE_LINE.items() if k != "section"}, "7")])   # a line with no section counts for every section
def test_the_log_reader_counts_every_analyze_that_may_have_reached_plex(env, line, section):
    log_at_clock(line)
    assert holds(section)


def test_backfill_skips_a_section_it_gave_up_on_at_once(env, monkeypatch, tmp_path, capsys):
    paths = [env["path"]] + [str(tmp_path / "media" / "Film A (1979)" / f"Other{n}.mkv") for n in (1, 2)]
    for n, p in enumerate(paths[1:], 1):
        open(p, "wb").write(b"x")
        env["plex_items"].append(plex_item(f"710{1 + n}", f"tmdb://{90001 + n}", p))
    movies = [{"id": 7 + n, "title": "Film A", "year": 1979, "originalLanguage": {"name": "English"}, "runtime": 120, "tmdbId": 90001 + n,
               "movieFile": {"path": p, "mediaInfo": {"audioStreamCount": 2}}} for n, p in enumerate(paths)]
    cap_checks = hook.PLEX_BUSY_CAP // hook.PLEX_BUSY_WAIT + 1
    env["activities"] = [[scan()]] * (cap_checks + 1)   # busy through the first file's cap and the second file's first check
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    real_run = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: None if argv[0] == "ionice" else real_run(argv, **k))
    hook.main(["--backfill", "radarr", "--apply"])
    assert analyzes(env) == ["/library/metadata/7103/analyze"]   # the third file, once the section is idle
    assert env["sleeps"] == [hook.PLEX_BUSY_WAIT] * 60 + [hook.PLEX_PACE] * 2 + [hook.PLEX_QUIET, hook.PLEX_PACE]
    done = [r for r in log_lines(env) if r.get("outcome") == "edited"]
    assert [r["plex_reason"] for r in done] == ["plex_analyze_skipped_busy", "plex_analyze_skipped_busy", "plex_analyze_sent"]
    assert "earlier skip" in done[1]["plex"]
    assert [(r["source"], r["plex_reason"]) for r in log_lines(env) if r["result"] == "plex_deferred"] == [("backfill", "plex_section_busy")]
    assert capsys.readouterr().out.count("waiting for Plex section 12 (Scanning Movies Film A)") == 1


# --- a backfill's burst: one two-check confirmation, then one fresh idle check per analyze ---

# Real shapes from Plex 1.43. An analyze makes Plex queue this work. It is no scan, so it never ends a burst.
LOUDNESS = {"cancellable": False, "progress": 50, "subtitle": "Show E S01 E05", "title": "Generating loudness data",
            "type": "media.generate.loudness", "userID": 1, "uuid": "00000000-0000-4000-8000-000000000002"}
CREDITS = {"cancellable": False, "progress": -1, "subtitle": "Show E S01 E21", "title": "Detecting Credits",
           "type": "media.generate.credits", "userID": 1, "uuid": "00000000-0000-4000-8000-000000000003"}
WATCH_SETTING = {"advanced": False, "default": False, "group": "library", "hidden": False, "id": "FSEventLibraryUpdatesEnabled",
                 "label": "Scan my library automatically", "type": "bool", "value": False,
                 "summary": "Your library will be updated automatically when changes to library folders are detected."}


def backfill_films(env, monkeypatch, tmp_path, n):
    """n films in section 12, each in Plex, for a backfill apply that edits every one."""
    paths = [env["path"]] + [str(tmp_path / "media" / "Film A (1979)" / f"Other{i}.mkv") for i in range(1, n)]
    for i, p in enumerate(paths[1:], 1):
        open(p, "wb").write(b"x")
        env["plex_items"].append(plex_item(f"710{1 + i}", f"tmdb://{90001 + i}", p))
    movies = [{"id": 7 + i, "title": "Film A", "year": 1979, "originalLanguage": {"name": "English"}, "runtime": 120,
               "tmdbId": 90001 + i, "movieFile": {"path": p, "mediaInfo": {"audioStreamCount": 2}}} for i, p in enumerate(paths)]
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    real_run = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: None if argv[0] == "ionice" else real_run(argv, **k))


@pytest.mark.parametrize("ignored", [False, True])
def test_a_ctrl_c_during_an_edit_of_a_backfill_ends_that_file_and_stops(env, monkeypatch, tmp_path, capsys, ignored):
    """Ctrl+C during the first file's mkvpropedit, in a one-worker apply. no_stop() holds the SIGINT until the write
    ends, and the backfill's handler sets stop. So the first file gets its decision line, the second never starts, and
    no KeyboardInterrupt prints a traceback. A process that ignores SIGINT edits both files."""
    backfill_films(env, monkeypatch, tmp_path, 2)
    real = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: (argv[0] == "mkvpropedit" and not env["mkvpropedit"]
                                                                   and os.kill(os.getpid(), signal.SIGINT)) or real(argv, **k))
    handler = signal.signal(signal.SIGINT, signal.SIG_IGN) if ignored else signal.getsignal(signal.SIGINT)
    try:
        hook.main(["--backfill", "radarr", "--apply"])
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C raised KeyboardInterrupt")
    finally:
        after = signal.signal(signal.SIGINT, handler)
    lines = [(r["result"], r["path"]) for r in log_lines(env) if r["result"] in ("editing", "edited")]
    assert lines[:2] == [("editing", env["path"]), ("edited", env["path"])] and len(lines) == (4 if ignored else 2), lines
    assert len(env["mkvpropedit"]) == len(lines) // 2 and f'summary: {{"edited": {len(lines) // 2}}}' in capsys.readouterr().out
    assert after == (signal.SIG_IGN if ignored else handler)


@pytest.mark.parametrize("watch, checks", [
    (False, [0, 15, 17, 19]),                                           # Plex does not watch the folders: one confirmation
    ([False, True], [0, 15, 17, 19, 34]),                               # the owner turns it on before the third file
    (True, [0, 15, 17, 32, 34, 49]),                                    # an edit can start a scan: two checks per file
    (None, [0, 15, 17, 32, 34, 49]),                                    # no such setting
    (urllib.error.URLError("refused"), [0, 15, 17, 32, 34, 49])])       # the read failed
def test_a_backfill_burst_confirms_the_section_once(env, monkeypatch, tmp_path, watch, checks):
    backfill_films(env, monkeypatch, tmp_path, 3)
    env["prefs"] = watch
    env["activities"] = [[LOUDNESS, CREDITS]] * 6   # Plex's own work after each analyze
    real_sleep, logged = hook.time.sleep, []
    def sleep(sec):   # the decision line with the analyze is in the log before the pause, so a folder scan elsewhere sees it
        if sec == hook.PLEX_PACE:
            logged.append((len(analyzes(env)), len([r for r in log_lines(env) if r.get("plex_section")])))
        real_sleep(sec)
    monkeypatch.setattr(hook.time, "sleep", sleep)
    hook.main(["--backfill", "radarr", "--apply"])
    assert logged == [(1, 1), (2, 2), (3, 3)]
    assert analyzes(env) == ["/library/metadata/7101/analyze", "/library/metadata/7102/analyze", "/library/metadata/7103/analyze"]
    assert env["checks"] == checks
    plex = [(m, urlparse(u).path) for m, u, b in env["http"] if "plex.invalid" in u]
    assert all(plex[i - 1] == ("GET", "/activities") for i, c in enumerate(plex) if c[0] == "PUT")   # a fresh check before each PUT
    assert [r["plex_reason"] for r in log_lines(env) if r.get("outcome") == "edited"] == ["plex_analyze_sent"] * 3


def burst_job(env, label="Film A"):
    return hook.plex_job("radarr", "backfill", label, env["path"], {"guids": ["tmdb://90001"], "title": "Film A"}, None)


def burst_run(env, gaps, burst):
    """plex_analyze() for one file after each gap in seconds, as a backfill sends them. Returns the reason codes."""
    codes = []
    for gap in gaps:
        hook.time.sleep(gap)
        codes.append(hook.plex_analyze(burst_job(env), set(), burst=burst)[1])
    return codes


def test_a_scan_mid_burst_ends_it(env):
    env["prefs"] = False
    env["activities"] = [[], [], [], [scan()]]   # the third file's check finds a scan that started after the second analyze
    burst, start = {}, env["clock"][0]
    assert burst_run(env, [0, hook.PLEX_PACE, hook.PLEX_PACE, hook.PLEX_PACE], burst) == ["plex_analyze_sent"] * 4
    assert env["checks"] == [0, 15, 17, 19, 49, 64, 66]   # busy at 19: wait, then two new idle checks, then the burst again
    assert len(analyzes(env)) == 4 and burst == {"12": (start + 49, start + 66)}   # the new row starts at 49
    assert [r["plex_reason"] for r in log_lines(env) if r["result"] == "plex_deferred"] == ["plex_section_busy"]
    env["activities"] = [[scan()]]   # a file in a section this run gave up on stops at its first busy check
    assert hook.plex_analyze(burst_job(env), {"12"}, burst=burst)[1] == "plex_analyze_skipped_busy" and burst == {}
    assert burst_run(env, [hook.PLEX_PACE], burst) == ["plex_analyze_sent"] and env["checks"][-3:] == [66, 68, 83]


def test_an_idle_gap_ends_the_burst(env):
    env["prefs"], burst = False, {}
    gaps = [0, hook.PLEX_QUIET + 1, hook.PLEX_QUIET]   # a slow file, then one that comes exactly PLEX_QUIET after a check
    assert burst_run(env, gaps, burst) == ["plex_analyze_sent"] * 3
    assert env["checks"] == [0, 15, 31, 46, 61]   # 16 s after the last check: two new checks. 15 s after: one.
    env["activities"] = [urllib.error.URLError("refused")]
    assert burst_run(env, [hook.PLEX_PACE], burst) == ["plex_analyze_sent"] and env["checks"][-3:] == [63, 93, 108]   # a failed check ends it too


def test_each_process_confirms_the_section_itself(env):
    """The hook's worker sends every analyze of its job processes itself, see coordinate(). It never uses a burst, because
    the import made the app ask Plex for a scan of the item's folder. A second backfill has its own burst. So an idle
    row that one backfill saw never releases an analyze of another sender."""
    env["prefs"], burst = False, {}
    burst_run(env, [0], burst)
    pending = [burst_job(env, "hook item")]
    drain(pending)
    assert env["checks"] == [0, 15, 15, 30]   # the worker: two checks, though the backfill's row is live
    burst_run(env, [0], {})
    assert env["checks"][-2:] == [30, 45]   # another backfill: its own two checks
    burst_run(env, [0], burst)   # the other senders' checks never extended this row. Its last check was at 15, so it has expired.
    assert env["checks"][-2:] == [45, 60] and len(analyzes(env)) == 4


# --- the backfill ------------------------------------------------------------------------------

def test_backfill_arguments(env, monkeypatch):
    movies = [{"id": i, "title": f"Movie {i}", "year": 2000, "originalLanguage": {"name": "English"}, "runtime": 120,
               "movieFile": {"path": env["path"], "mediaInfo": {"audioStreamCount": 2}}} for i in (5, 101)]
    seen = []
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    monkeypatch.setattr(hook, "process", lambda ctx: seen.append(ctx.label) or {"outcome": "no_change", "result": "no change"})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: None)
    hook.main(["--backfill", "radarr", "--ids", "101", "--limit", "5", "--apply"])
    assert seen == ["Movie 101 (2000)"]
    with pytest.raises(SystemExit) as ex:
        hook.main(["--backfill", "radarr", "--limit"])
    assert ex.value.code == 2


def test_backfill_apply_writes_the_undo_record_before_a_failed_reprobe(env, monkeypatch):
    movies = [{"id": 7, "title": "Film A", "year": 1979, "originalLanguage": {"name": "English"}, "runtime": 120,
               "movieFile": {"path": env["path"], "mediaInfo": {"audioStreamCount": 2}}}]
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    probes = iter([copy.deepcopy(PORTUGUESE_DEFAULT)] * 2)   # the probe that selects the file, then the one the edit starts from
    monkeypatch.setattr(hook, "mkvmerge", lambda p: next(probes))
    real_run = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: None if argv[0] == "ionice" else real_run(argv, **k))
    hook.main(["--backfill", "radarr", "--apply"])
    lines = log_lines(env)
    assert [r["result"] for r in lines][0] == "editing" and lines[0]["undo"]   # the undo record, then the decision line with the error
    assert [(r["outcome"], r["source"]) for r in lines[1:]] == [("error", "backfill")]
    assert "lock" in env["events"]


def test_backfill_analyzes_one_item_at_a_time(env, monkeypatch, tmp_path):
    second = tmp_path / "media" / "Film A (1979)" / "Other.mkv"
    second.write_bytes(b"x")
    movies = [{"id": i, "title": "Film A", "year": 1979, "originalLanguage": {"name": "English"}, "runtime": 120, "tmdbId": 90001 + n,
               "movieFile": {"path": p, "mediaInfo": {"audioStreamCount": 2}}} for n, (i, p) in enumerate(((7, env["path"]), (8, str(second))))]
    env["plex_items"].append(plex_item("7102", "tmdb://90002", str(second)))
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    real_run = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: None if argv[0] == "ionice" else real_run(argv, **k))
    hook.main(["--backfill", "radarr", "--apply"])
    assert analyzes(env) == ["/library/metadata/7101/analyze", "/library/metadata/7102/analyze"]
    assert env["sleeps"] == [hook.PLEX_QUIET, hook.PLEX_PACE] * 2   # one lookup, two idle checks, then a pause
    assert env["checks"] == [0, 15, 17, 32]


# --- broken audio --------------------------------------------------------------------------------

SILENCE = "[x] [info] n_samples: 1922128\n[x] [info] max_volume: -91.0 dB\n"
GRAB = {"records": [{"id": 2102, "eventType": "downloadFolderImported"}, {"id": 2101, "eventType": "grabbed"}]}


def grabbed(env, monkeypatch, app="radarr"):
    monkeypatch.setenv(f"{app}_download_id", "a1b2c3d4")
    monkeypatch.setenv("radarr_moviefile_id", "11")
    env["movies"]["history?downloadId=a1b2c3d4&pageSize=1000"] = GRAB


def test_silent_audio_on_import_deletes_remonitors_and_fails_the_grab(env, monkeypatch):
    env["ffmpeg_out"] = [SILENCE]
    monkeypatch.setenv("radarr_moviefile_id", "11")
    grabbed(env, monkeypatch)
    hook.main([])
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "history/failed/2101", None)]
    assert env["mkvpropedit"] == [] and analyzes(env) == []   # no flag edit on a file about to go
    (rec,) = log_lines(env)
    assert rec["outcome"] == "broken_audio"
    f, act = action(env)
    assert (f["kind"], f["certain"], hook.action_code(act)) == ("audio", "the audio is silent at all 3 places checked", "regrabbed")
    assert act == {"code": "regrabbed", "name": "Radarr", "kind": "audio", "n": 1, "came": [], "linked": True, "own_copy": True, "others": 0,
                   "stayed": None}
    # the samples decode the track that plays after the flag decision: English, the second audio track
    assert {a[a.index("-map") + 1] for a in env["ffmpeg"]} == {"0:a:1"}
    assert len(env["ffmpeg"]) == 6   # three samples, then three more from scratch before the delete


def test_sonarr_regrab_remonitors_the_episodes(env, monkeypatch):
    as_sonarr(monkeypatch, env, {"title": "Show", "originalLanguage": {"name": "English"}}, [{"seasonNumber": 1, "episodeNumber": 2, "runtime": 44}])
    real = hook.arr
    eps = [{"id": 31, "episodeFileId": 9}, {"id": 32, "episodeFileId": 9}]
    monkeypatch.setattr(hook, "arr", lambda app, p: GRAB if p.startswith("history?") else eps if p.startswith("episode?episodeIds") else real(app, p))
    monkeypatch.setenv("sonarr_download_id", "d759")
    monkeypatch.setenv("sonarr_episodefile_episodeids", "31,32")
    env["ffmpeg_out"] = [SILENCE]
    hook.main([])
    assert env["writes"] == [("DELETE", "episodefile/9", None), ("PUT", "episode/monitor", {"episodeIds": [31, 32], "monitored": True}),
                             ("POST", "history/failed/2101", None)]


def test_regrab_cap_alerts_only(env, monkeypatch):
    env["ffmpeg_out"] = [SILENCE]
    grabbed(env, monkeypatch)
    hook.store.put("regrabs", "radarr", [env["clock"][0] - 60] * hook.CFG.regrab_cap)
    hook.store.put("regrabs", "sonarr", [])
    hook.main([])
    assert env["writes"] == []
    assert log_lines(env)[0]["findings"] == [
        {"kind": "audio", "certain": "the audio is silent at all 3 places checked", "action": {"code": "capped", "cap": hook.CFG.regrab_cap}}]
    assert hook.CFG.regrab_cap == 30


def test_a_unit_of_another_instance_on_the_same_download_id_never_counts(env, monkeypatch):
    """Two instances grab one torrent, so both see one download id. Sonarr's unit of it, failed and with file 11 deleted,
    must neither skip Radarr's file 11 as deleted with its download nor let Radarr's re-grab pass its cap uncounted."""
    env["ffmpeg_out"] = [SILENCE]
    grabbed(env, monkeypatch)
    hook.store.put("unit", "sonarr|a1b2c3d4", {"time": env["clock"][0], "failed": True, "deleted": [11], "clean": [], "kind": "audio"})
    hook.store.put("regrabs", "radarr", [env["clock"][0] - 60] * hook.CFG.regrab_cap)
    hook.main([])
    (rec,) = [r for r in log_lines(env) if r.get("outcome")]
    assert rec["findings"][0]["action"] == {"code": "capped", "cap": hook.CFG.regrab_cap} and env["writes"] == []
    hook.store.drop("regrabs")
    env["writes"].clear()
    hook.main([])
    assert ("DELETE", "moviefile/11", None) in env["writes"] and len(hook.store.get("regrabs", "radarr")) == 1   # counted, and failed once
    assert hook.store.get("unit", "radarr|a1b2c3d4")["deleted"] == [11] and hook.store.get("unit", "sonarr|a1b2c3d4")["deleted"] == [11]


def test_manual_import_alerts_only(env):
    env["ffmpeg_out"] = [SILENCE]
    hook.main([])
    assert env["writes"] == [] and env["mkvpropedit"] == []
    assert log_lines(env)[0]["findings"][0]["action"] == {"code": "no_grab", "name": "Radarr"}


def test_one_silent_sample_alerts_and_still_edits(env, monkeypatch):
    env["ffmpeg_out"] = [SILENCE, env["ffmpeg_out"][0], env["ffmpeg_out"][0]]
    grabbed(env, monkeypatch)
    hook.main([])
    assert env["writes"] == [] and len(env["mkvpropedit"]) == 1
    (rec,) = [r for r in log_lines(env) if r.get("outcome") == "edited"]
    assert rec["findings"] == [{"kind": "audio", "doubts": ["the audio is silent at 1 of 3 places checked"]}]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Made-up files for real ffmpeg: a tone, digital silence, audio that stops halfway, damaged bytes, a file cut at
    60 percent, and an mp4."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    d = tmp_path_factory.mktemp("media")
    lav = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=120", "-f", "lavfi"]
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]
    subprocess.run(lav + ["-i", "sine=frequency=440:duration=120"] + enc + [str(d / "good.mkv")], check=True)
    subprocess.run(lav + ["-i", "anullsrc=r=48000:cl=stereo", "-t", "120"] + enc + [str(d / "silent.mkv")], check=True)
    subprocess.run(lav + ["-i", "sine=frequency=440:duration=60"] + enc + [str(d / "short.mkv")], check=True)
    subprocess.run(lav + ["-i", "sine=frequency=440:duration=120"] + enc + ["-movflags", "+faststart", str(d / "good.mp4")], check=True)
    shutil.copy(d / "good.mkv", d / "corrupt.mkv")
    with open(d / "corrupt.mkv", "r+b") as f:
        size = os.path.getsize(d / "corrupt.mkv")
        for share in (0.1, 0.5, 0.85):
            f.seek(int(size * share)); f.write(bytes((i * 7919) % 256 for i in range(20000)))
    shutil.copy(d / "good.mkv", d / "cut.mkv")
    os.truncate(d / "cut.mkv", int(os.path.getsize(d / "good.mkv") * 0.6))
    subprocess.run(lav[:7] + ["testsrc=size=160x90:rate=10:duration=90", "-f", "lavfi", "-i", "sine=frequency=440:duration=90"] + enc
                   + [str(d / "tail.mkv")], check=True)
    os.truncate(d / "tail.mkv", os.path.getsize(d / "tail.mkv") - 3000)   # only the last 3 KB are missing
    return d


def probe(path):
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                                     capture_output=True, text=True, check=True).stdout)


def test_detection_on_generated_media(media):
    got = {n: hook.check_audio(str(media / f"{n}.mkv"), probe(media / f"{n}.mkv"), [], runtime=2)[0]
           for n in ("good", "silent", "cut", "corrupt", "short")}
    assert got == {"good": None, "silent": "the audio is silent at all 3 places checked", "cut": "the file is cut off before the last place checked",
                   "corrupt": "the audio fails to play at all 3 places checked", "short": None}   # audio shorter than video is only a doubt
    assert "no audio plays at the last place checked" in hook.check_audio(str(media / "short.mkv"), probe(media / "short.mkv"), [])[1][0]


def test_a_file_missing_only_its_tail_is_never_certain(media):
    certain, doubts, samples = hook.check_audio(str(media / "tail.mkv"), probe(media / "tail.mkv"), [])
    assert samples[2]["cut"] and samples[2]["n"] > 0   # ffmpeg logs the premature end, the late sample still decodes
    assert certain is None and "the file may end early, near the last place checked" in doubts


def test_a_lost_tail_under_an_inflated_header_is_never_certain(media):
    j = probe(media / "tail.mkv"); j["format"]["duration"] = str(float(j["format"]["duration"]) * 1.05)
    certain, doubts, samples = hook.check_audio(str(media / "tail.mkv"), j, [])
    assert samples[2]["cut"] and 0 < samples[2]["n"]   # the late window runs past the real end and reads short
    assert certain is None and "the file may end early, near the last place checked" in doubts
    cut = hook.check_audio(str(media / "cut.mkv"), probe(media / "cut.mkv"), [], runtime=2)
    assert cut[0] == "the file is cut off before the last place checked" and cut[2][2]["n"] == 0


def test_a_cut_needs_the_listed_runtime_to_agree(media):
    """A 90-second file under a 120-second header: the late sample starts past the content, so no cut is certain."""
    j = probe(media / "tail.mkv"); j["format"]["duration"] = "120"
    assert hook.check_audio(str(media / "tail.mkv"), j, [], runtime=1.5)[0] is None
    assert hook.check_audio(str(media / "tail.mkv"), j, [])[0] is None   # no listed runtime, no certain cut
    assert hook.check_audio(str(media / "cut.mkv"), probe(media / "cut.mkv"), [], runtime=2)[0] == "the file is cut off before the last place checked"


def test_a_certain_fault_needs_ffprobe_to_count_the_same_tracks(media):
    one = {"container": {"properties": {"duration": 120 * 10**9}},
           "tracks": [{"type": "audio", "properties": {"language": "eng", "default_track": True, "uid": 1}}]}
    assert hook.check_audio(str(media / "silent.mkv"), one, [])[0] == "the audio is silent at all 3 places checked"
    two = {"container": {"properties": {"duration": 120 * 10**9}},
           "tracks": [one["tracks"][0], {"type": "audio", "properties": {"language": "por", "default_track": False, "uid": 2}}]}
    certain, doubts, _ = hook.check_audio(str(media / "silent.mkv"), two, [])   # mkvmerge lists 2, ffprobe sees 1
    assert certain is None and doubts[-1].startswith("the audio is silent at all 3 places checked, but this is unconfirmed, because two tools count 1 and 2")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_an_unreadable_file_is_never_certain(media, tmp_path):
    f = tmp_path / "locked.mkv"
    shutil.copy(media / "silent.mkv", f)
    j = probe(f)
    os.chmod(f, 0)
    assert hook.check_audio(str(f), j, [])[:2] == (None, ["the audio check could not run at 3 of 3 places"])


def test_an_audio_index_ffmpeg_lacks_is_never_certain(media):
    j = {"container": {"properties": {"duration": 120 * 10**9}},
         "tracks": [{"type": "audio", "properties": {"language": "por", "default_track": False, "uid": 1}},
                    {"type": "audio", "properties": {"language": "eng", "default_track": True, "uid": 2}}]}   # the file has one
    assert hook.check_audio(str(media / "silent.mkv"), j, [])[:2] == (None, ["the audio check could not run at 3 of 3 places"])


def test_an_inflated_duration_header_is_never_certain(media):
    j = probe(media / "good.mkv"); j["format"]["duration"] = "48213"   # 13:23:33, far past the real end
    certain, doubts, _ = hook.check_audio(str(media / "good.mkv"), j, [])
    assert certain is None and "no audio plays at the last place checked, maybe because the file says it runs longer than it does" in doubts


def test_declared_channels_never_set_the_expected_length(media):
    j = probe(media / "good.mkv")
    next(x for x in j["streams"] if x["codec_type"] == "audio")["channels"] = 6   # the container says 6, ffmpeg decodes 1
    assert hook.check_audio(str(media / "good.mkv"), j, [])[:2] == (None, [])


def test_mkvmerge_without_audio_needs_ffprobe_to_agree(media, monkeypatch):
    j = {"container": {"properties": {"duration": 120 * 10**9}}, "tracks": [{"type": "video", "properties": {}}]}
    assert hook.check_audio(str(media / "good.mp4"), j, [])[:2] == (None, ["one tool finds no audio track, but another finds one"])
    monkeypatch.setattr(hook, "ffprobe_audio", lambda p: [])
    assert hook.check_audio(str(media / "good.mp4"), j, [])[0] == "the file has no audio track"


# --- the checks after the three samples: rules 1 to 8 of the audio scan --------------------------------------------
# Each record below is the shape of a real file, or of a trial file. The generated files repeat each
# shape at two minutes. Without ffmpeg, ffprobe or mkvtoolnix the tests on real files skip.

def mkv_record(duration, app, tracks):
    """An mkvmerge -J record. tracks are (type, codec, DURATION tag, frames, default duration in ns)."""
    return {"container": {"recognized": True, "supported": True, "properties": {"duration": int(duration * 1e9), "writing_application": app}},
            "tracks": [{"type": t, "codec": c, "properties": {"language": "eng", "default_track": True, "uid": i + 1, "tag_duration": tag,
                                                               "tag_number_of_frames": frames, "default_duration": dd, "tag__statistics_writing_app": app}}
                       for i, (t, c, tag, frames, dd) in enumerate(tracks)]}


def sampled(at, n=5_760_000, peak=-12.0):
    return {"at": at, "window": 20, "n": n, "max": peak if n else None, "errors": 0, "ran": True, "cut": False, "rate": 48000, "ch": 6}


def packets(**kw):
    """A packet_read() result of a whole 2-hour file with no fault."""
    return dict({"codec": "eac3", "profile": None, "audio": 7200.0, "video": 7200.0, "video_gap": 0.1, "held": 7200.0, "hole": [10.0, 10.03],
                 "subs": [], "took": 1.0}, **kw)


def test_samples_sit_on_the_stream_ends_under_a_runaway_subtitle():
    """One SubRip event sets the Segment duration to 54:07, and the video and the audio end at 21:38."""
    runaway = mkv_record(3247.4, "mkvmerge v80.0 ('Name A') 64-bit", [("video", "AVC/H.264/MPEG-4p10", "00:21:38.312000000", "31127", 41708333),
                                                                     ("audio", "E-AC-3", "00:21:38.336000000", "40573", 32000000)])
    assert hook.audio_span("/nonexistent.mkv", runaway, 0) == pytest.approx(1298.336)
    runaway["tracks"][1]["properties"]["tag__statistics_writing_app"] = "Lavf60.3.100"   # a copied tag counts for nothing
    runaway["tracks"][0]["properties"]["tag__statistics_writing_app"] = "Lavf60.3.100"
    assert hook.audio_span("/nonexistent.mkv", runaway, 0) == pytest.approx(3247.4)


def test_audio_that_stops_at_the_start_is_certain_and_a_stray_timeline_never_is():
    """6.3 s of audio packets, 46:26 of video. The tags still say 46:26 for the audio."""
    quiet = [sampled(279, 0), sampled(1393, 0), sampled(2368, 0)]
    assert hook.arr_decide.stops_early(quiet, packets(audio=6.3, video=2786.4)) == "the audio stops at 0:06, but the video runs to 46:26"
    long_header = [sampled(7056, 0), sampled(35280, 0), sampled(59976, 0)]   # a 19.6 h header, both streams end at 21:05
    assert hook.arr_decide.stops_early(long_header, packets(audio=1265.3, video=1265.2)) is None
    assert hook.arr_decide.stops_early(quiet[:2] + [sampled(2368)], packets(audio=6.3, video=2786.4)) is None   # one sample heard audio
    stray = [sampled(2011, 0), sampled(10055, 0), sampled(17094, 0)]   # trial: one video packet 20,000 s late in 2:00
    assert hook.arr_decide.stops_early(stray, packets(audio=120.0, video=20110.1, video_gap=20000.0)) is None


def test_a_constant_rate_track_that_holds_too_little_is_a_doubt():
    """The audio track holds 46:05 of audio for its 61:43 of video, with holes of up to 188 s."""
    gappy = packets(held=2765.3, video=3702.536, audio=3580.2, hole=[1152.4, 1340.4])
    assert hook.arr_decide.held(gappy) == "the audio track holds only 46:05 of sound for 1:01:42 of video"
    assert hook.arr_decide.held(dict(gappy, held=3690.0)) is None
    assert hook.arr_decide.held(dict(gappy, codec="aac")) is None and hook.arr_decide.held(dict(gappy, codec="dts", profile="DTS-HD MA")) is None
    assert hook.arr_decide.held(dict(gappy, video_gap=40.0)) is None   # trial: a joined capture whose streams jump 40 s
    rec = mkv_record(3702.536, "mkvmerge v82.0 ('Name B') 64-bit", [("video", "AVC/H.264/MPEG-4p10", "01:01:42.536000000", "221931", 16683333),
                                                                     ("audio", "E-AC-3", "00:59:40.224000000", "86417", 32000000)])
    assert hook.arr_decide.tag_held(rec, 0) == pytest.approx(2765.344) and hook.arr_decide.cbr(rec, 0)


def test_a_short_file_with_a_hole_and_decode_errors_is_certain():
    """The whole track decodes 118.6 of its 171.4 s, with 6 decode errors."""
    partial = {"kind": "full", "ran": True, "decoded": 118.6, "end": 171.4, "errors": 6}
    assert hook.arr_decide.full_verdict(partial) == ("only 1:58 of the 2:51 audio track plays, with 6 errors", None)
    assert hook.arr_decide.full_verdict(dict(partial, errors=0)) == (None, "only 1:58 of the 2:51 audio track plays")
    assert hook.arr_decide.full_verdict(dict(partial, decoded=171.4)) == (None, None)   # all of it
    assert hook.arr_decide.full_verdict(dict(partial, ran=False)) == (None, None)


def test_silent_end_credits_need_a_text_dialogue_track_that_ends_before_them():
    """Silent from 15:22, the late sample at 15:41, and the last subtitle event ends at 15:05."""
    credits = [sampled(111), sampled(554), sampled(941, peak=-91.0)]
    assert hook.arr_decide.credits_silence(credits, [[905.4, 287, True], [905.1, 271, True]], 1107) == 905.4
    assert hook.arr_decide.credits_silence(credits, [], 1107) is None                               # no subtitles: the doubt stays
    assert hook.arr_decide.credits_silence(credits, [[905.4, 12, True]], 1107) is None              # forced signs only
    assert hook.arr_decide.credits_silence(credits, [[905.4, 90, False]], 1107) is None             # PGS: 2 packets an event
    assert hook.arr_decide.credits_silence(credits, [[905.4, 287, True], [1010.0, 20, False]], 1107) is None   # an event runs into the silence
    middle = [sampled(66), sampled(330, peak=-91.0), sampled(561)]   # a silent middle sample is no credits
    assert hook.arr_decide.credits_silence(middle, [[650.0, 300, True]], 660) is None


def test_the_tags_answer_before_any_packet_read(monkeypatch):
    """A packet read runs only when a rule needs it. The silent credits of the test above need it for rule 1. The
    same file with a subtitle DURATION tag past the late sample, or with PGS subtitles only, needs no read."""
    app = "mkvmerge v84.0 ('Name C') 64-bit"
    rec = mkv_record(1107.2, app, [("video", "AVC/H.264/MPEG-4p10", "00:18:27.200000000", "26573", 41666666),
                                   ("audio", "E-AC-3", "00:18:27.200000000", "34600", 32000000),
                                   ("subtitles", "SubRip/SRT", "00:15:03.000000000", "287", None)])
    rec["tracks"][2]["properties"]["codec_id"] = "S_TEXT/UTF8"
    reads = []
    monkeypatch.setattr(hook, "packet_read", lambda path, index: reads.append(path) or packets(audio=1107.2, video=1107.2, held=1107.2, subs=[[905.4, 287, True]]))
    credits = [sampled(111), sampled(554), sampled(941, peak=-91.0)]
    doubts = ["the audio is silent at 1 of 3 places checked"]
    assert hook.audio_more("/x.mkv", rec, 0, credits, None, doubts, 1107.2)[:2] == (None, []) and len(reads) == 1
    rec["tracks"][2]["properties"]["tag_duration"] = "00:15:50.000000000"   # an event ends past 15:41: the doubt stays, no read
    assert hook.audio_more("/x.mkv", rec, 0, credits, None, doubts, 1107.2)[1] == doubts and len(reads) == 1
    rec["tracks"][2]["properties"].update(codec_id="S_HDMV/PGS", tag_duration="00:15:03.000000000")
    assert hook.audio_more("/x.mkv", rec, 0, credits, None, doubts, 1107.2)[1] == doubts and len(reads) == 1
    lost = [sampled(111), sampled(554, n=2_000_000), sampled(941)]   # a sample lost audio: tags from the mux miss later damage
    assert hook.audio_more("/x.mkv", rec, 0, lost, None, ["part of the audio is missing, and only 35% of it plays where it was checked"], 1107.2)[0] is None and len(reads) == 2


def test_a_read_that_cannot_finish_in_time_is_skipped_and_logged(monkeypatch, tmp_path):
    """A 60 GB 4k remux with 270 s of the job's time limit left: the video check keeps its 220 s, and the scan reads it later."""
    remux = tmp_path / "remux.mkv"
    with open(remux, "wb") as f:
        f.truncate(60 * 10**9)   # sparse
    monkeypatch.setattr(hook.DEADLINE, "left", lambda: 270.0)
    assert hook.AUDIO_RESERVE >= hook.ZERO_SECS + 3 * hook.arr_decide.VIDEO_TIMEOUT + hook.VIDEO_RESERVE
    assert hook.packet_read(str(remux), 0) == {"error": "the packet read skipped: 60.0 GB needs about 600 s, and 50 s are left"}
    assert hook.full_decode(str(remux), 0)["error"].startswith("the full decode skipped: 60.0 GB")
    monkeypatch.setattr(hook.DEADLINE, "left", lambda: None)   # a scan: no time limit
    assert hook.read_time(str(remux), "the packet read") == (hook.AUDIO_MAX, None)
    assert hook.packet_read(str(tmp_path / "gone.mkv"), 0) == {"error": "the packet read skipped: the file cannot be read, FileNotFoundError"}


def test_only_a_file_with_no_media_signature_counts_as_unreadable():
    noise = bytes.fromhex("935de222cecc1c4ad3704ba98a607f92ed5445ea91a455ed62fa9787192e09b20f9e329bdb76e5ee5937b9dba4309f6d0bde08db254babf7346797054211bf11")
    assert not hook.arr_decide.media_signature(noise)   # random bytes
    for head in (b"\x1a\x45\xdf\xa3\x01", b"\x00\x00\x00\x20ftypisom", b"RIFF\x00\x00\x00\x00AVI ", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", b"\x47\x40\x11"):
        assert hook.arr_decide.media_signature(head)


UNREAD = {"container": {"recognized": False, "supported": False}, "errors": [], "warnings": []}


def random_bytes(path):
    path.write_bytes(bytes((i * 7919 + 13) % 251 + 1 for i in range(65536)))   # no EBML header
    return str(path)


def test_a_missing_tool_is_unknown_and_never_certain(monkeypatch, tmp_path):
    """CI has no ffprobe. A missing tool says nothing about the file, so random bytes stay a doubt there."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    noise = random_bytes(tmp_path / "Show M - s01e03.mkv")
    assert hook.check_audio(noise, UNREAD, [])[:2] == (None, ["ffprobe did not run: FileNotFoundError"])
    assert hook.full_decode(noise, 0) == {"kind": "full", "ran": False, "error": "ffmpeg did not finish: FileNotFoundError"}
    assert hook.packet_read(noise, 0) == {"error": "ffprobe did not run: FileNotFoundError"}


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs ffprobe")
def test_a_file_no_tool_reads_is_certain_only_with_no_media_signature(tmp_path):
    noise = random_bytes(tmp_path / "Show M - s01e03.mkv")
    assert hook.check_audio(noise, UNREAD, [])[0] == "no tool can read the file, and it does not start like any known video format"
    iso = tmp_path / "disc.iso"
    iso.write_bytes(open(noise, "rb").read())
    assert hook.check_audio(str(iso), UNREAD, [])[:2] == (None, ["the file cannot be read properly, and a second tool failed too"])
    ebml = tmp_path / "broken.mkv"
    ebml.write_bytes(b"\x1a\x45\xdf\xa3" + open(noise, "rb").read())
    assert hook.check_audio(str(ebml), UNREAD, [])[0] is None


@pytest.fixture(scope="module")
def shapes(tmp_path_factory):
    """Two-minute files with AC-3 audio in the shapes of the real cases and the trials."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg, ffprobe and mkvmerge")
    d = tmp_path_factory.mktemp("shapes")
    vid = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=120", "-f", "lavfi"]
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", "-b:a", "192k"]
    run = lambda *argv: subprocess.run(list(argv), check=True)
    run(*vid, "-i", "sine=frequency=440:duration=4", *enc, str(d / "stops.mkv"))   # the audio stops at 0:04
    run(*vid, "-i", "sine=frequency=440:duration=120", "-af", "aselect='not(between(t,20,80))'", *enc, str(d / "hole.mkv"))   # no packets 0:20-1:20
    run(*vid, "-i", "sine=frequency=440:duration=120", *enc, str(d / "damaged.mkv"))
    size = os.path.getsize(d / "damaged.mkv")
    with open(d / "damaged.mkv", "r+b") as f:   # damage between the samples, found only by a full decode
        f.seek(int(size * 0.2)); f.write(bytes((i * 7919 + 13) % 256 for i in range(int(size * 0.2))))
    for name, last in (("early", 95), ("late", 112)):   # a SubRip event every 5 s up to last
        (d / f"{name}.srt").write_text("".join(f"{i + 1}\n00:{t // 60:02d}:{t % 60:02d},000 --> 00:{(t + 3) // 60:02d}:{(t + 3) % 60:02d},000\n"
                                               f"Line {i}\n\n" for i, t in enumerate(range(1, last, 5))))
    credits = [*vid, "-i", "sine=frequency=440:duration=100,apad=whole_dur=120"]   # digital silence from 1:40, the credits
    run(*credits, "-i", str(d / "early.srt"), "-map", "0", "-map", "1", "-map", "2", *enc, "-c:s", "srt", str(d / "credits.mkv"))
    run(*credits, "-i", str(d / "late.srt"), "-map", "0", "-map", "1", "-map", "2", *enc, "-c:s", "srt", str(d / "credits_late.mkv"))
    run(*credits, *enc, str(d / "credits_nosub.mkv"))
    run(*vid[:7], "testsrc=size=160x90:rate=10:duration=40", "-f", "lavfi", "-i", "sine=frequency=440:duration=40", *enc, str(d / "tiny.mkv"))
    # the trials: a video packet 20,000 s late, a TS that starts at PTS 1,000 s, a joined TS whose streams jump
    # 40 s, an AC-3 track that drops from 384 to 192 kbit/s, and a track that starts 15 s late by design
    run(*vid, "-i", "sine=frequency=440:duration=120", *enc, str(d / "base.mkv"))
    run("ffmpeg", "-v", "error", "-y", "-i", str(d / "base.mkv"), "-map", "0", "-c", "copy", "-bsf:v", "setts=ts='if(eq(N,1100),TS+20000/TB,TS)'",
        "-f", "matroska", str(d / "stray_raw.mkv"))
    run("mkvmerge", "-q", "-o", str(d / "stray.mkv"), str(d / "stray_raw.mkv"))
    run("ffmpeg", "-v", "error", "-y", "-i", str(d / "base.mkv"), "-map", "0", "-c", "copy",   # a broken stts entry: the last frame lasts 20,000 s
        "-bsf:v", "setts=duration='if(eq(N,1199),20000/TB,DURATION)'", str(d / "lastdur.mp4"))
    run("ffmpeg", "-v", "error", "-y", "-i", str(d / "lastdur.mp4"), "-map", "0", "-c", "copy", str(d / "lastdur.mkv"))
    run(*vid, "-i", "sine=frequency=440:duration=120", *enc, "-output_ts_offset", "1000", str(d / "offset.ts"))
    for n, at in ((1, 1000), (2, 1120)):
        run(*vid[:7], "testsrc=size=160x90:rate=10:duration=80", "-f", "lavfi", "-i", "sine=frequency=440:duration=80", *enc,
            "-output_ts_offset", str(at), str(d / f"part{n}.ts"))
    (d / "joined.ts").write_bytes((d / "part1.ts").read_bytes() + (d / "part2.ts").read_bytes())
    for name, rate, secs in (("a", "384k", 30), ("b", "192k", 90)):
        run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}", "-c:a", "ac3", "-b:a", rate, "-ac", "2", str(d / f"{name}.ac3"))
    run(*vid[:7], "testsrc=size=160x90:rate=10:duration=120", "-i", f"concat:{d / 'a.ac3'}|{d / 'b.ac3'}", "-map", "0", "-map", "1",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", str(d / "switch.mkv"))
    run(*vid[:7], "testsrc=size=160x90:rate=10:duration=120", "-itsoffset", "15", "-f", "lavfi", "-i", "sine=frequency=440:duration=105",
        "-map", "0", "-map", "1", *enc, str(d / "late.mkv"))
    return d


def test_the_real_shapes_on_generated_files(shapes):
    got = {n: hook.check_audio(str(shapes / f"{n}.mkv"), probe(shapes / f"{n}.mkv"), [])
           for n in ("stops", "hole", "damaged", "credits", "credits_late", "credits_nosub")}
    assert got["stops"][0] == "the audio stops at 0:04, but the video runs to 2:00"
    assert got["hole"][0] == "the audio track holds only 1:00 of sound for 2:00 of video, and its 60 s gap at 0:20 plays nothing"
    assert got["hole"][2][-1]["kind"] == "hole" and got["hole"][2][-2]["hole"] == [pytest.approx(20, abs=0.1), pytest.approx(80, abs=0.1)]
    assert got["damaged"][0].startswith("only 1:") and got["damaged"][0].endswith(" errors")
    assert got["credits"][:2] == (None, []) and got["credits"][2][2]["credits"] == pytest.approx(94, abs=0.1)
    assert got["credits_late"][:2] == got["credits_nosub"][:2] == (None, ["the audio is silent at 1 of 3 places checked"])
    certain, doubts, (full,) = hook.check_audio(str(shapes / "tiny.mkv"), probe(shapes / "tiny.mkv"), [])
    assert (certain, doubts) == (None, []) and full["kind"] == "full" and full["decoded"] == pytest.approx(full["end"], abs=0.2)


def test_the_trials_are_never_certain(shapes):
    """Each trial file plays whole. None may reach a certain verdict, and none a held doubt."""
    stray = hook.check_audio(str(shapes / "stray.mkv"), REAL_MKVMERGE(str(shapes / "stray.mkv")), [])   # its tags carry the stray end
    assert stray[0] is None and stray[2][-1]["video_gap"] == pytest.approx(20000, abs=1)   # before the fix: "the video runs to 335:20"
    for n in ("lastdur.mp4", "lastdur.mkv"):   # the same end from one frame's duration, with no gap between packets
        last = hook.check_audio(str(shapes / n), REAL_MKVMERGE(str(shapes / n)), [])
        assert last[0] is None and last[2][-1]["kind"] == "packets" and last[2][-1]["video_gap"] == pytest.approx(20000, abs=1), n
    assert hook.packet_read(str(shapes / "base.mkv"), 0)["video_gap"] < 1
    offset = hook.packet_read(str(shapes / "offset.ts"), 0)
    assert offset["audio"] == pytest.approx(120, abs=0.1) and offset["video"] == pytest.approx(120, abs=0.1)   # before: 18:41 of video
    joined = hook.packet_read(str(shapes / "joined.ts"), 0)
    assert joined["video_gap"] == pytest.approx(40, abs=0.1) and hook.arr_decide.held(joined) is None
    switch = hook.packet_read(str(shapes / "switch.mkv"), 0)
    assert switch["held"] == pytest.approx(120, abs=0.2) and hook.arr_decide.held(switch) is None   # before: 1:13 of audio
    late = hook.full_decode(str(shapes / "late.mkv"), 0)
    assert late["decoded"] == pytest.approx(late["end"], abs=0.2) and hook.arr_decide.full_verdict(late) == (None, None)
    for n in ("offset.ts", "joined.ts", "switch.mkv"):
        assert hook.check_audio(str(shapes / n), probe(shapes / n), [])[:2] == (None, []), n


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_an_mp4_or_avi_is_sampled_on_the_ffprobe_duration(media):
    """mkvmerge gives no duration for AVI, MP4 and TS, so the samples sit on the ffprobe duration."""
    j = {"container": {"recognized": True, "supported": True, "type": "QuickTime/MP4", "properties": {}},
         "tracks": [{"type": "video", "properties": {}}, {"type": "audio", "properties": {"language": "eng", "default_track": True, "uid": 2}}]}
    certain, doubts, samples = hook.check_audio(str(media / "good.mp4"), j, [])
    assert (certain, doubts) == (None, []) and [s["at"] for s in samples[:3]] == [12, 60, 102] and all(s["n"] for s in samples[:3])


def test_a_file_the_app_replaces_during_a_scan_is_counted_gone(env, monkeypatch, tmp_path, capsys):
    """The app deletes a file between the library read and its check. It counts as gone, never as a CHECK line."""
    files = scan_library(monkeypatch, tmp_path, 2)

    def check(path, j, edits, runtime=0):
        if path == files[0]:
            os.remove(path)
            raise RuntimeError(f"mkvmerge: The file '{path}' could not be opened for reading: open file error.")
        return None, ["the audio is silent at 1 of 3 places checked"], []
    monkeypatch.setattr(hook, "check_audio", check)
    hook.main(["--backfill", "radarr", "--check-audio"])
    base = os.path.join(hook.CFG.state_dir, "audio-scan-radarr")
    assert scan_state(base)["checked"] == 2
    assert [r["file_id"] for r in scan_rows(base)] == [102]
    assert [r["ids"]["file_id"] for r in log_lines(env)] == [102]
    (post,) = [b["embeds"][0] for m, u, b in env["http"] if m == "POST"]
    assert post["description"] == "1 files checked this run, 2 of 2 in this pass. Files that left the library during their check: 1."
    assert "GONE    Movie 1 (2000)" in capsys.readouterr().out


def test_a_second_check_that_disagrees_keeps_the_file(env, monkeypatch):
    env["ffmpeg_out"] = [SILENCE] * 3 + env["ffmpeg_out"] * 3   # certain first, clean from scratch
    grabbed(env, monkeypatch)
    hook.main([])
    assert env["writes"] == []
    assert log_lines(env)[0]["findings"][0]["action"] == {"code": "unconfirmed"}
    assert not hook.store.items("regrabs")


def scan_state(base):
    """Where the scan of base, STATE_DIR/<kind>-scan-<app>, stands in its pass, from the store."""
    return hook.store.get("scan", os.path.basename(base))


def scan_rows(base):
    """The problems the scan of base found in its pass, in file id order."""
    return sorted(hook.store.items(os.path.basename(base)).values(), key=lambda r: r["file_id"])


def test_audio_scan_resumes_and_posts_a_summary_only_for_a_run_with_a_problem(env, monkeypatch, tmp_path):
    scan_library(monkeypatch, tmp_path, 3)
    env["ffmpeg_out"] = [SILENCE] * 3 + env["ffmpeg_out"] * 6   # file 1 silent, files 2 and 3 fine
    real_run = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: None if argv[0] == "ionice" else real_run(argv, **k))
    hook.main(["--backfill", "radarr", "--check-audio", "--limit", "2"])
    base = os.path.join(hook.CFG.state_dir, "audio-scan-radarr")
    assert scan_state(base)["last"] == 102
    hook.main(["--backfill", "radarr", "--check-audio"])
    assert scan_state(base)["checked"] == 3 and len(env["ffmpeg"]) == 9
    posts = [b["embeds"][0] for m, u, b in env["http"] if m == "POST"]
    assert len(posts) == 1 and posts[0]["title"] == f"Audio scan: Radarr {hook.CFG.instance}" and posts[0]["color"] == hook.COLORS["amber"]
    assert dict((f["name"], f["value"]) for f in posts[0]["fields"])["Problems this run"] == "1"
    assert posts[0]["description"] == "2 files checked this run, 2 of 3 in this pass."
    assert open(base + ".txt").read().startswith("BROKEN\tMovie 1 (2000)\tthe audio is silent at all 3 places checked")
    assert env["writes"] == [] and env["mkvpropedit"] == []   # read-only: no edit, no re-grab, only a decision line per file
    assert {(r["source"], r["outcome"]) for r in log_lines(env)} <= {("audio_scan", "audio_checked"), ("audio_scan", "broken_audio")}
    assert env["sleeps"].count(hook.SCAN_PACE) == 3 and env["events"].count("lock") == 3


def test_worker_cleans_old_temp_jobs_and_warns_on_a_backlog(env, monkeypatch):
    os.makedirs(hook.queue_dir(), exist_ok=True)
    stale = os.path.join(hook.queue_dir(), ".123-1.json")
    open(stale, "w").close()
    os.utime(stale, (0, 0))
    running = hook.try_lock("worker.lock")
    hook.main([])
    running.close()
    env["clock"][0] += 7200
    hook.worker(hook.try_lock("worker.lock"))
    assert not os.path.exists(stale)
    lines = log_lines(env)
    assert lines[0]["result"] == "warning" and "over an hour" in lines[0]["note"]


# --- a download is one re-grab unit ---------------------------------------------------------------

def pack(env, monkeypatch, tmp_path, broken, other=None):
    """A 10-episode season pack from one download. Episodes 101-110 hold files 201-210. broken are episode numbers 1-10.
    other maps an episode id to another series id, as when a manual import maps a file to another series."""
    other = other or {}
    folder = tmp_path / "media" / "Show" / "Season 1"
    folder.mkdir(parents=True)
    paths = {200 + n: str(folder / f"Show - s01e{n:02d}.mkv") for n in range(1, 11)}
    for p in paths.values():
        open(p, "wb").write(b"x")
    records = [{"id": 900, "eventType": "grabbed", "episodeId": 101}] + [
        {"id": 900 + n, "eventType": "downloadFolderImported", "episodeId": 100 + n, "data": {"fileId": str(200 + n)}} for n in range(1, 11)]
    series = {"title": "Show", "originalLanguage": {"name": "English"}}

    def arr(app, p):
        if p == "series/5": return series
        if p.startswith("history?"): return {"records": records}
        if p.startswith("episode?episodeFileId="):
            n = int(p.rsplit("=", 1)[1]) - 200
            return [{"id": 100 + n, "seasonNumber": 1, "episodeNumber": n, "runtime": 22}]
        if p.startswith("episode?episodeIds="):
            return [{"id": int(i), "episodeFileId": int(i) + 100, "seriesId": other.get(int(i), 5)} for i in parse_qs(p.split("?", 1)[1])["episodeIds"]]
        if p.startswith("episodefile/"): return {"id": int(p.split("/")[1]), "path": paths[int(p.split("/")[1])]}
        raise AssertionError(p)
    monkeypatch.setattr(hook, "arr", arr)
    bad = {paths[200 + n] for n in broken}
    real_run = hook.subprocess.run
    def run(argv, **k):   # silence for the broken files, a tone for the rest
        if argv[0] == "ffmpeg":
            env["ffmpeg"].append(argv)
            out = SILENCE if argv[argv.index("-i") + 1] in bad else "[x] [info] n_samples: 1922128\n[x] [info] max_volume: -4.0 dB\n"
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": out})()
        return real_run(argv, **k)
    monkeypatch.setattr(hook.subprocess, "run", run)
    for k in ("radarr_eventtype", "radarr_movie_id", "radarr_moviefile_path"):
        monkeypatch.delenv(k)
    return paths


def import_event(monkeypatch, paths, fid):
    for k, v in (("sonarr_eventtype", "Download"), ("sonarr_series_id", "5"), ("sonarr_episodefile_id", str(fid)),
                 ("sonarr_episodefile_episodeids", str(fid - 100)), ("sonarr_episodefile_path", paths[fid]), ("sonarr_download_id", "pack1")):
        monkeypatch.setenv(k, v)
    hook.main([])


def regrabs_counted(app="sonarr"):
    return len(hook.store.get("regrabs", app))


def test_pack_with_three_broken_files_is_one_regrab(env, monkeypatch, tmp_path):
    paths = pack(env, monkeypatch, tmp_path, broken=(1, 4, 7))
    import_event(monkeypatch, paths, 201)
    deletes = [p for m, p, b in env["writes"] if m == "DELETE"]
    assert deletes == ["episodefile/201", "episodefile/204", "episodefile/207"]
    assert [b for m, p, b in env["writes"] if m == "PUT"] == [{"episodeIds": [101, 104, 107], "monitored": True}]
    assert [p for m, p, b in env["writes"] if m == "POST"] == ["history/failed/900"]
    assert [m for m, p, b in env["writes"]] == ["DELETE"] * 3 + ["PUT", "POST"]   # every delete before the failed mark
    assert regrabs_counted() == 1 and len(env["ffmpeg"]) == 39   # each of the 10 files once, each broken one again
    assert log_lines(env)[-1]["findings"][0] == {"kind": "audio", "certain": "the audio is silent at all 3 places checked", "action": {
        "code": "regrabbed", "name": "Sonarr", "kind": "audio", "n": 3, "came": [], "linked": True, "own_copy": True, "others": 0, "stayed": None}}


def test_pack_with_only_the_first_file_broken_still_fails_the_grab_once(env, monkeypatch, tmp_path):
    paths = pack(env, monkeypatch, tmp_path, broken=(1,))
    import_event(monkeypatch, paths, 201)
    assert env["writes"] == [("DELETE", "episodefile/201", None), ("PUT", "episode/monitor", {"episodeIds": [101], "monitored": True}),
                             ("POST", "history/failed/900", None)]
    assert regrabs_counted() == 1


def test_later_jobs_of_a_handled_download_are_skipped_or_only_edited(env, monkeypatch, tmp_path):
    paths = pack(env, monkeypatch, tmp_path, broken=(1, 4))
    import_event(monkeypatch, paths, 201)
    writes, samples = len(env["writes"]), len(env["ffmpeg"])
    os.makedirs(os.path.dirname(paths[204]), exist_ok=True); open(paths[204], "wb").write(b"x")   # the app deletes it, the fake does not
    import_event(monkeypatch, paths, 204)   # deleted with its download
    import_event(monkeypatch, paths, 202)   # sampled clean with its download
    assert len(env["writes"]) == writes and len(env["ffmpeg"]) == samples   # no second delete, failed mark or sample
    last = log_lines(env)
    assert [(r.get("outcome"), r.get("fault")) for r in last if r.get("path") == paths[204]][-1] == ("deleted_with_download", "audio")
    clean = [r for r in last if r.get("path") == paths[202] and r["result"] != "plex"][-1]
    assert clean["outcome"] == "edited" and "audio already checked with its download" in clean["notes"]
    assert regrabs_counted() == 1


def test_a_straggler_of_a_failed_download_is_deleted_without_a_second_failed_mark(env, monkeypatch, tmp_path):
    paths = pack(env, monkeypatch, tmp_path, broken=(1, 9))
    hook.store.put("unit", "sonarr|pack1", {"time": env["clock"][0], "failed": True, "deleted": [201], "clean": [202, 203]})   # the unit ran before file 209 was imported
    import_event(monkeypatch, paths, 209)
    assert [m for m, p, b in env["writes"]] == ["DELETE", "PUT"] and ("DELETE", "episodefile/209", None) in env["writes"]
    assert log_lines(env)[-1]["findings"][0]["action"] == {"code": "regrabbed", "name": "Sonarr", "kind": "audio", "n": 1, "failed_before": True,
                                                           "came": [], "linked": True, "own_copy": True, "others": 0, "stayed": None}
    assert not hook.store.items("regrabs")   # no second count


def test_an_ids_audio_scan_keeps_its_own_state(env, monkeypatch, tmp_path):
    scan_library(monkeypatch, tmp_path, 3)
    env["ffmpeg_out"] = [SILENCE]
    real_run = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: None if argv[0] == "ionice" else real_run(argv, **k))
    hook.main(["--backfill", "radarr", "--check-audio", "--limit", "1"])
    base = os.path.join(hook.CFG.state_dir, "audio-scan-radarr")
    full = (scan_state(base), scan_rows(base))
    hook.main(["--backfill", "radarr", "--check-audio", "--ids", "3"])
    assert (scan_state(base), scan_rows(base)) == full
    assert scan_state(base + "-ids")["last"] == 103
    assert not [n for n in os.listdir(hook.CFG.state_dir) if n.endswith(".tmp")]


def test_a_timeout_while_sampling_a_pack_uses_no_cap_count(env, monkeypatch, tmp_path):
    paths = pack(env, monkeypatch, tmp_path, broken=(1, 4))
    real = hook.check_audio
    def check(path, j, edits, runtime=0):
        if path == paths[203]:
            raise hook.arr_meta.OutOfTime("stopped after 300 seconds")
        return real(path, j, edits, runtime)
    monkeypatch.setattr(hook, "check_audio", check)
    import_event(monkeypatch, paths, 201)
    assert env["writes"] == [] and log_lines(env)[-1]["result"].startswith("error: OutOfTime")
    assert not hook.store.items("regrabs")


def test_a_failed_mkvpropedit_still_records_the_flags_it_left(env, monkeypatch):
    """mkvpropedit can exit 2 on the Tracks element, yet the file reads fine with the intended flags."""
    real = hook.subprocess.run
    def exits_2(argv, **kw):
        r = real(argv, **kw)   # the fixture's mkvpropedit changes the flags
        if argv[0] == "mkvpropedit":
            return type("R", (), {"returncode": 2, "stdout": "Updating the 'Tracks' element failed. The file has been modified.", "stderr": ""})()
        return r
    monkeypatch.setattr(hook.subprocess, "run", exits_2)
    hook.main([])
    rec = [r for r in log_lines(env) if r.get("outcome") == "edit_failed"][0]
    assert [t["default"] for t in rec["after"] if t["pos"] in ("a1", "a2", "s1")] == [0, 1, 0]
    assert rec["findings"][-1] == {"kind": "edit", "error": "mkvpropedit failed: Updating the 'Tracks' element failed. The file has been modified.",
                                   "unread": None, "on": ["a2 eng"]}


def test_a_failed_mkvpropedit_that_breaks_the_file_says_so(env, monkeypatch):
    real = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: type("R", (), {"returncode": 2, "stdout": "", "stderr": "bad"})()
                        if argv[0] == "mkvpropedit" else real(argv, **kw))
    probes = iter([copy.deepcopy(PORTUGUESE_DEFAULT)])
    def probe(p):
        try:
            return next(probes)
        except StopIteration:
            raise RuntimeError("mkvmerge: not a Matroska file")
    monkeypatch.setattr(hook, "mkvmerge", probe)
    hook.main([])
    rec = [r for r in log_lines(env) if r.get("outcome") == "edit_failed"][0]
    assert rec["after_error"] == "RuntimeError: mkvmerge: not a Matroska file" and "after" not in rec
    assert rec["findings"][-1] == {"kind": "edit", "error": "mkvpropedit failed: bad", "unread": "RuntimeError: mkvmerge: not a Matroska file",
                                   "on": None}




def test_a_posted_alert_has_one_field_and_masks_the_secrets(env):
    """alert_findings() posts one embed per finding, with the title and the file in one field. tests/test_report.py holds the
    title and the color of each finding."""
    rec = dict(app="radarr", label="Film A (1979)", path="/m/other.mkv",
               findings=[{"kind": "edit", "error": "It failed on https://discord.invalid/ops with plex-t0ken-1234.", "unread": None, "on": None}])
    assert hook.alert_findings(rec, 1) == ["sent"]
    e = env["http"][-1][2]["embeds"][0]
    color = hook.COLORS[hook.title(rec["findings"][0])[1]]   # the finding's own color
    assert (e["color"], [(f["name"], f["value"]) for f in e["fields"]]) == (color, [("Film A (1979)", "other.mkv")])
    assert "discord.invalid" not in json.dumps(e) and "plex-t0ken-1234" not in json.dumps(e) and "<DISCORD_WEBHOOK>" in e["description"]


def test_a_clean_scan_posts_nothing(env, monkeypatch, tmp_path, capsys):
    f = tmp_path / "media" / "m1.mkv"; f.write_bytes(b"x")
    monkeypatch.setattr(hook, "arr", lambda app, p: [{"id": 1, "title": "Movie", "year": 2000, "originalLanguage": {"name": "English"},
                                                      "movieFile": {"id": 101, "path": str(f)}}])
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    real_run = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: None if argv[0] == "ionice" else real_run(argv, **k))
    hook.main(["--backfill", "radarr", "--check-audio"])
    assert [u for m, u, b in env["http"] if m == "POST"] == [] and "0 with a problem this run, 0 in this pass" in capsys.readouterr().out


def test_a_maximal_embed_stays_under_discords_limit(env):
    long = "x" * 5000
    e = hook.embed("radarr", long, long, "red", [("Title", long), ("File", long), ("Action", long), ("App", long)])
    total = len(e["title"]) + len(e["description"]) + len(e["footer"]["text"]) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"])
    assert total <= 6000 and len(e["description"]) == 2000 and len(e["title"]) == 256 and len(e["fields"]) == 4
    assert all(len(f["value"]) <= 1024 for f in e["fields"])


def test_several_audio_doubts_become_one_alert(env, monkeypatch):
    monkeypatch.setattr(hook, "check_audio", lambda *a: (None, ["the audio is silent at 1 of 3 places checked", "no audio plays at an earlier place checked"], []))
    hook.main([])
    rec = [r for r in log_lines(env) if r.get("outcome") == "edited"][0]
    assert [f for f in rec["findings"] if f["kind"] == "audio"] == [
        {"kind": "audio", "doubts": ["the audio is silent at 1 of 3 places checked", "no audio plays at an earlier place checked"]}]
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1


# --- the decision on real files -------------------------------------------------------------------
# tests/fixtures/records.json holds trimmed mkvmerge -J records of real files, with the flags each file
# had before the backfill edited it. The expected flags are the corrective commands of a backfill. expect_edits is an
# exact plan, and expect_abstain is the code of the reason a record must abstain. spoken is TMDB's answer, absent when unknown.

RECORDS = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "records.json")))


@pytest.mark.parametrize("rec", RECORDS, ids=[f'{r["class"]} {r["name"][:40]}' for r in RECORDS])
def test_rules_on_real_records(rec):
    d = hook.arr_decide.decide(rec["probe"], rec["original"], rec["kids"], rec.get("release", ""), spoken=rec.get("spoken"))
    ts = d["tracks"]
    final = {t["sel"]: t["default"] for t in ts}
    final.update(hook.arr_decide.defaults(d["edits"]))
    assert d["abstain"] == rec.get("expect_abstain"), d
    if "expect_edits" in rec:
        assert sorted([e[0], e[1]] + e[3:] for e in d["edits"]) == sorted(rec["expect_edits"]), d   # a forced edit: [sel, 0, "flag-forced"]
    for sel, flag in rec.get("expect_flags", {}).items():
        assert final[sel] == flag, (sel, d)
    if "expect_audio" in rec:
        plays = hook.arr_decide.default_audio(ts, d["edits"])
        english_on = any(final[t["sel"]] for t in ts if t["kind"] == "s" and t["lang"] == "eng" and t["role"] != "forced")
        assert (plays["lang"], english_on) == (rec["expect_audio"], rec["expect_english_sub"]), d
    if d["edits"]:   # every plan that edits keeps the invariants, and its class names its path and rules
        assert hook.arr_decide.invariants(ts, d["edits"], d["cls"], set(d["orig"])) == []
        assert hook.arr_decide.plan_class(d).startswith(d["path"] + ": ")


# --- the forced-flag clear ---------------------------------------------------------------------------
# A forced English track with 10 or more events a minute loses its forced flag when English is the
# only audio language. A dense forced track under the other full subtitles' median keeps it (foreign-dialogue films).
# In an English original TMDB must also list English as the only spoken language. A file whose TMDB entry lists
# en, pt and es keeps the flag.
ENG_ONLY = ["eng"]

def dense(*subs, audio=(("eng", ""),)):
    """100 minutes, mkvmerge statistics. subs are (lang, title, default, forced flag, frames)."""
    tracks = [mk("audio", lang, i + 1, i == 0, track_name=title) for i, (lang, title) in enumerate(audio)]
    for i, (lang, title, dflt, forced, frames) in enumerate(subs):
        t = mk("subtitles", lang, 10 + i, dflt, track_name=title, tag_number_of_frames=str(frames))
        t["properties"]["forced_track"] = forced
        tracks.append(t)
    return {"container": {"properties": {"duration": 6000 * 10**9, "writing_application": "mkvmerge v92.0"}}, "tracks": tracks}


def test_a_dense_forced_english_track_loses_its_forced_flag_under_english_only_audio():
    A = hook.arr_decide
    d = A.decide(dense(("eng", "English", True, True, 1500)), "English", spoken=ENG_ONLY)   # 15 events a minute
    assert d["edits"] == [["track:=10", 0, 1], ["track:=10", 0, 1, "flag-forced"]] and not d["undecided"], d
    assert "forced_flag_cleared_dense" in d["reasons"] and A.plan_class(d) == "English original: English subtitle off, forced flag cleared"
    # not default already: the plan clears only the forced flag, because Plex shows a forced track without the default flag
    d = A.decide(dense(("eng", "English", False, True, 1500)), "English", spoken=ENG_ONLY)
    assert d["edits"] == [["track:=10", 0, 1, "flag-forced"]] and A.plan_class(d) == "English original: forced flag cleared", d
    assert A.default_audio(d["tracks"], d["edits"])["pos"] == "a1" and A.defaults(d["edits"]) == {}


def test_the_forced_clear_keeps_the_flag_where_it_may_be_real():
    A = hook.arr_decide
    # under 10 events a minute the decision still abstains
    assert A.decide(dense(("eng", "English", True, True, 900)), "English")["abstain"] == "dense_forced_flag_english_only"
    # a title that says forced keeps the flag, and so does a signs title
    assert A.decide(dense(("eng", "Forced", True, True, 1500), ("eng", "", False, False, 1500)), "English")["edits"] == []
    # a second main audio language: the flag is for its viewers, so only the default goes off
    d = A.decide(dense(("eng", "English_Full", True, True, 1500), audio=(("eng", ""), ("hin", ""))), "English")
    assert d["edits"] == [["track:=10", 0, 1]], d
    # the guard: 11 events a minute against a full English track of 18 is a real forced track of a film with much foreign
    # dialogue. It keeps the flag, and the plan abstains as before.
    d = A.decide(dense(("eng", "English", True, True, 1100), ("eng", "English SDH", False, False, 1800)), "English", spoken=ENG_ONLY)
    assert d["edits"] == [] and d["abstain"] == "dense_forced_flag_english_only" and "forced_flag_kept_reference" in d["reasons"], d
    # at 0.8 of the median of the other full subtitles, of any language, the flag goes (11.5 against 11.5)
    d = A.decide(dense(("eng", "English", True, True, 1150), ("eng", "English (SDH)", False, False, 1440), ("fre", "", False, False, 1150),
                       ("ara", "", False, False, 1150)), "English", spoken=ENG_ONLY)
    assert ["track:=10", 0, 1, "flag-forced"] in d["edits"], d


def test_an_english_original_clears_the_forced_flag_only_when_tmdb_lists_english_alone():
    A = hook.arr_decide
    cc_forced = dense(("eng", "CC", True, True, 1620), ("eng", "English", True, False, 1310))   # an English original, before the backfill
    # TMDB unknown or down: the flag stays, and the plan abstains, with a reason code that names TMDB
    for spoken, code, why in ((None, "forced_flag_kept_tmdb_unknown", "because TMDB is unknown"),
                              (["eng", "por", "spa"], "forced_flag_kept_spoken", "TMDB lists the spoken languages eng, por, spa"),
                              ([], "forced_flag_kept_spoken", "TMDB lists no spoken language")):
        d = A.decide(cc_forced, "English", spoken=spoken)
        assert d["edits"] == [] and d["abstain"] == "dense_forced_flag_english_only" and code in d["reasons"] and why in d["undecided"], d
    # a dense forced track that is not default keeps its flag too, with no edit and no abstain
    d = A.decide(dense(("eng", "English", False, True, 1500)), "English")
    assert d["edits"] == [] and not d["undecided"] and d["reasons"] == ["forced_flag_kept_tmdb_unknown"], d
    # a foreign original that plays its English dub needs no TMDB answer
    dubbed = dense(("eng", "CC", True, True, 1620), ("eng", "English", True, False, 1310), audio=(("eng", "English"),))
    assert A.decide(dubbed, "French")["edits"] == [["track:=10", 0, 1], ["track:=11", 0, 1], ["track:=10", 0, 1, "flag-forced"]]


def test_the_forced_clear_reads_the_policy():
    A = hook.arr_decide
    cc_forced, hindi = dense(("eng", "CC", True, True, 1620), ("eng", "English", True, False, 1310)), \
        dense(("eng", "English_Full", True, True, 1500), audio=(("eng", ""), ("hin", "")))
    try:
        A.set_policy(dict(POLICY, forced_clear=dict(POLICY["forced_clear"], events=20)))
        assert A.decide(cc_forced, "English", spoken=ENG_ONLY)["abstain"] == "dense_forced_flag_english_only"
        A.set_policy(dict(POLICY, forced_clear=dict(POLICY["forced_clear"], english_only_audio=False)))
        assert ["track:=10", 0, 1, "flag-forced"] in A.decide(hindi, "English", spoken=ENG_ONLY)["edits"]
        with pytest.raises(ValueError, match="forced_clear.reference_ratio"):
            A.set_policy(dict(POLICY, forced_clear={"events": 10, "english_only_audio": True}))
    finally:
        A.set_policy(POLICY)
    assert A.decide(cc_forced, "English", spoken=ENG_ONLY)["edits"] == [["track:=10", 0, 1], ["track:=11", 0, 1], ["track:=10", 0, 1, "flag-forced"]]


@pytest.mark.parametrize("tmdb, code", [(None, "forced_flag_kept_tmdb_unknown"), (dict(spoken=["eng", "por", "spa"]), "forced_flag_kept_spoken")])
def test_the_hook_keeps_the_forced_flag_when_tmdb_is_unknown_or_lists_other_languages(env, tmdb, code):
    env["probe"] = dense(("eng", "CC", True, True, 1620), ("eng", "English", True, False, 1310))   # an English original, before the backfill
    env["tmdb"] = tmdb and dict(TMDB, **tmdb)
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "undecided" and rec["abstain"] == "dense_forced_flag_english_only" and code in rec["reasons"], rec
    assert env["mkvpropedit"] == []


def test_the_hook_clears_the_forced_flag_with_an_undo_and_plans_nothing_more(env, monkeypatch):
    env["probe"] = dense(("eng", "English", True, True, 1500))
    env["tmdb"], asks = dict(TMDB), []
    monkeypatch.setattr(hook.arr_meta, "expected_languages", lambda *a, **k: asks.append(a) or env["tmdb"])
    hook.main([])
    assert len(asks) == 1   # the decision asks TMDB, and the metadata checks reuse the answer
    assert env["mkvpropedit"] == [["--edit", "track:=10", "--set", "flag-default=0", "--edit", "track:=10", "--set", "flag-forced=0"]]
    editing, rec = log_lines(env)[:2]
    undo = ["mkvpropedit", env["path"], "--edit", "track:=10", "--set", "flag-default=1", "--edit", "track:=10", "--set", "flag-forced=1"]
    assert editing["result"] == "editing" and editing["undo"] == undo and rec["undo"] == undo
    assert rec["outcome"] == "edited" and rec["recheck"] == {"edits": 0, "undecided": None, "invariants": []}
    assert rec["edit_rules"] == ["English subtitle off", "forced flag cleared"] and "forced_flag_cleared_dense" in rec["reasons"]
    (s1,) = [t for t in rec["after"] if t["pos"] == "s1"]
    assert (s1["default"], s1["forced"]) == (0, False)
    hook.main([])   # the second import of the same file plans nothing more
    assert len(env["mkvpropedit"]) == 1 and log_lines(env)[-1]["outcome"] == "no_change"
    # the undo restores the file exactly. The fake mkvpropedit wants the file's last line in the log to be an editing line.
    hook.log({"result": "editing", "path": env["path"]})
    hook.subprocess.run(undo)
    assert env["files"][env["path"]] == dense(("eng", "English", True, True, 1500))


def test_the_role_default_policy_is_valid_and_a_bad_one_is_refused():
    hook.arr_decide.set_policy(POLICY)
    for bad in ({k: v for k, v in POLICY.items() if k != "kids"}, dict(POLICY, audio=dict(POLICY["audio"], foreign=["origin"]))):
        with pytest.raises(ValueError):
            hook.arr_decide.set_policy(bad)
    assert hook.arr_decide.POLICY is POLICY


def test_a_malformed_policy_never_stops_the_import_of_the_script(tmp_path, monkeypatch):
    bad = tmp_path / "policy.json"
    bad.write_text('{"kids": 1, "audio": "not a table", "subtitles": [], "sparse_events": 1, "forced_flag_events": 4,'
                   ' "density_min_minutes": 15, "min_confidence": 0.7,'
                   ' "forced_clear": {"events": 10, "english_only_audio": true, "reference_ratio": 0.8}}')
    envfile = tmp_path / "env"
    envfile.write_text(f"POLICY_FILE='{bad}'\n")
    monkeypatch.setenv("ARR_MEDIA_GUARD_ENV", str(envfile))
    before = hook.arr_decide.POLICY   # another test in this process may have loaded an equal policy anew
    fresh = amg.load("arr_media_guard_bad")   # AttributeError inside set_policy, caught at import
    assert fresh.POLICY_ERROR.startswith("AttributeError") and hook.arr_decide.POLICY is before


def test_a_missing_policy_alerts_once_and_skips(env, monkeypatch):
    monkeypatch.setattr(hook.arr_decide, "POLICY", None)
    monkeypatch.setattr(hook, "POLICY_ERROR", "FileNotFoundError: no such file")
    hook.main([]); hook.main([])
    recs = log_lines(env)
    assert [r["outcome"] for r in recs] == ["no_policy", "no_policy"] and env["mkvpropedit"] == []
    posts = [b for m, u, b in env["http"] if m == "POST"]
    assert len(posts) == 1 and posts[0]["embeds"][0]["title"] == hook.title({"kind": "policy"})[0]   # the policy alert
    with pytest.raises(SystemExit):
        hook.main(["--backfill", "radarr"])


def test_selftest_fails_without_a_live_policy(monkeypatch, settings, tmp_path):
    settings(state_dir=str(tmp_path), log=str(tmp_path / "log.jsonl"))   # --selftest records the policy in status.json
    monkeypatch.setattr(hook.arr_decide, "POLICY", None)
    with pytest.raises(SystemExit) as ex:
        hook.main(["--selftest"])
    assert "no policy loaded" in str(ex.value)


def test_an_undecided_import_edits_nothing_and_logs_why(env):
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Spanish"}   # the only audio is tagged eng, untitled
    env["probe"] = {"container": {"properties": {"duration": 7200 * 10**9}},
                    "tracks": [mk("video", "und", 1, True, pixel_dimensions="1920x1080"), mk("audio", "eng", 2, True, audio_channels=6),
                               mk("subtitles", "eng", 3, True)]}
    hook.main([])
    rec = log_lines(env)[-1]
    assert rec["outcome"] == "undecided" and rec["undecided"].startswith("the app says Spanish") and rec["class"] == "undecided", rec
    assert env["mkvpropedit"] == [] and analyzes(env) == []


def plan_line(label, cls, edits=(), undecided=None, tracks=None):
    tracks = tracks or [{"kind": "a", "sel": "track:=1", "pos": "a1", "lang": "eng", "role": "main", "extra": False, "default": 1},
                        {"kind": "s", "sel": "track:=2", "pos": "s1", "lang": "eng", "role": "full", "extra": False, "default": 1}]
    return {"app": "radarr", "label": label, "path": f"/m/{label}.mkv", "class": cls, "edits": [list(e) for e in edits], "undecided": undecided,
            "dropped": [], "cls": "english", "orig": ["eng"], "tracks": tracks}


def test_audit_of_a_dry_run_groups_classes_and_posts_one_embed(env, tmp_path, capsys):
    off = [["track:=2", 0, 1]]
    rows = [plan_line(f"Film {i}", "English original: English subtitle off", off) for i in range(3)]
    rows += [plan_line("Film D", "undecided", undecided="the app says Spanish, but the only audio is tagged eng and its title names no language"),
             plan_line("Bad (2020)", "English original: full English subtitle on", [["track:=2", 1, 0]],
                       tracks=[dict(plan_line("x", "")["tracks"][0]), dict(plan_line("x", "")["tracks"][1], default=0)]),
             plan_line("Clean (2021)", "no change")]
    plans = tmp_path / "plans.jsonl"
    plans.write_text("".join(json.dumps(r) + "\n" for r in rows))
    hook.main(["--audit", "radarr", "--plan-from", str(plans), "--post"])
    out = capsys.readouterr().out
    assert "4 of 6 files would change, in 2 classes. 1 undecided, 0 dropped by an invariant. 1 break an invariant on re-check." in out
    assert "3 English original: English subtitle off (Film 0; Film 1)" in out
    assert "1 full English subtitle s1 would stay on under English audio (Bad (2020))" in out
    posts = [b for m, u, b in env["http"] if m == "POST"]
    assert len(posts) == 1 and posts[0]["embeds"][0]["title"] == "Plan audit: Radarr " + hook.CFG.instance


def test_audit_since_reads_the_check_after_each_edit(env, capsys):
    hook.main([])   # the hook edits Film A: English audio plays, the Portuguese subtitle goes off
    rec = log_lines(env)[-2]
    assert rec["outcome"] == "edited" and rec["recheck"] == {"edits": 0, "undecided": None, "invariants": []}
    probes = len(env["events"])
    hook.main(["--audit", "radarr", "--since", "24h", "--source", "hook"])
    out = capsys.readouterr().out
    assert "1 files edited since" in out and "0 of them plan a further edit" in out and "Every plan keeps the invariants." in out
    assert "1 English original: audio switched, foreign subtitle off (Film A (1979))" in out
    assert "lock" not in env["events"][probes:]   # read from the log, the file was not probed again
    assert env["syslog"][-1].startswith("arr=radarr source=audit outcome=summary edited=1 further=0") and env["syslog"][-1].endswith("tmdb=ok")


def test_audit_since_probes_an_edit_logged_without_its_check(env, capsys):
    hook.main([])
    ((rowid, rec),) = hook.store.read("SELECT rowid, rec FROM decisions")   # the decision line, as the store keeps it
    hook.store.write("UPDATE decisions SET rec = ? WHERE rowid = ?", json.dumps({k: v for k, v in json.loads(rec).items() if k != "recheck"}),
                     rowid)   # a line from before the check existed
    env["files"][env["path"]]["tracks"][1]["properties"]["default_track"] = True   # someone put the Portuguese audio back first
    hook.main(["--audit", "radarr", "--since", "24h"])
    assert "1 of them plan a further edit" in capsys.readouterr().out
    assert log_lines(env)[-1]["source"] == "audit" and log_lines(env)[-1]["recheck"]["edits"] == 1   # a1 loses its flag again


def test_a_quiet_night_posts_nothing(env, capsys):
    hook.main(["--audit", "sonarr", "--since", "24h", "--source", "hook", "--post"])
    assert "0 files edited since" in capsys.readouterr().out
    assert [b for m, u, b in env["http"] if m == "POST"] == []


def test_a_canary_takes_files_across_classes():
    plans = [{"class": c, "path": f"{c}{i}"} for c, n in (("a", 5), ("b", 1), ("c", 2)) for i in range(n)]
    assert [p["path"] for p in hook.spread(plans, 4)] == ["a0", "c0", "b0", "a1"]
    assert len(hook.spread(plans, 100)) == 8


def test_backfill_writes_plans_and_a_canary_applies_only_from_them(env, monkeypatch, tmp_path, capsys):
    movie = dict(env["movies"]["movie/7"], id=7, qualityProfileId=1, movieFile={"id": 70, "path": env["path"], "sceneName": "Film.A.1979",
                                                                          "mediaInfo": {"audioStreamCount": 2}})
    other = tmp_path / "media" / "Other (2000).mkv"
    other.write_bytes(b"x")
    env["files"][str(other)] = copy.deepcopy(NO_ENGLISH)
    movie2 = dict(movie, id=8, title="Other", year=2000, movieFile={"id": 80, "path": str(other), "mediaInfo": {"audioStreamCount": 2}})
    env["movies"]["movie"] = [movie, movie2]
    plans = tmp_path / "plans.jsonl"
    hook.main(["--backfill", "radarr", "--plan-out", str(plans)])
    rows = [json.loads(line) for line in plans.read_text().splitlines()]
    # Other (2000) has one audio track and no subtitle, so the backfill never reads it, whatever its stale mediaInfo says
    assert [(r["label"], r["class"]) for r in rows] == [("Film A (1979)", "English original: audio switched, foreign subtitle off")]
    assert env["mkvpropedit"] == []
    with pytest.raises(SystemExit):
        hook.main(["--backfill", "radarr", "--canary", "1"])   # a canary needs the dry run's plans and --apply
    hook.main(["--backfill", "radarr", "--apply", "--plan-from", str(plans), "--canary", "1"])
    assert len(env["mkvpropedit"]) == 1 and "1 mkv files" in capsys.readouterr().out


def test_every_decision_line_carries_the_schema_the_classification_and_a_syslog_summary(env):
    hook.main([])
    rec = [r for r in log_lines(env) if r.get("outcome")][0]
    assert (rec["schema"], rec["source"], rec["outcome"], rec["item_class"], rec["ids"]["app_id"]) == (hook.SCHEMA, "hook", "edited", "english", "7")
    assert rec["policy"] == hook.policy_hash() and len(rec["policy"]) == 12 and rec["took"] >= 0
    assert rec["version"] == hook.own_version() and len(rec["version"]) == 12   # the hash of the deployed script and module
    assert rec["size"] == 1000 and rec["mtime"] > 0 and rec["file_duration"] == 7200 and rec["ids"]["guids"] == ["tmdb://90001", "imdb://tt9000001"]
    assert [t["i"] for t in rec["tracks"]] == ["a1", "a2", "s1"] and rec["tracks"][0]["role"] == "main" and rec["tracks"][0]["conf"] == 0.6
    assert rec["edit_rules"] == ["audio switched", "audio switched", "foreign subtitle off"] and rec["recheck"]["edits"] == 0
    assert rec["audio"]["certain"] is None and len(rec["audio"]["samples"]) == 3
    assert rec["tmdb"] == "no_record" and rec["trusted"]["trust"] == "agree" and rec["evidence"]["regrab"] is False
    line = env["syslog"][0]
    assert line.startswith('arr=radarr source=hook outcome=edited class="English original: audio switched, foreign subtitle off" edits=3 ')
    assert 'label="Film A (1979)"' in line and "tmdb=no_record" in line and "plex-t0ken-1234" not in line and "tracks" not in line and len(line) < 400


def test_a_decision_line_keeps_the_code_its_result_came_with(env):
    """The code comes from where the result happened, so a text that starts like the text of another code keeps its
    own. The line holds it after host, and a result with no code is other."""
    cases = [("wrong content unconfirmed: x", "wrong_content_unconfirmed"), ("wrong content: x", "wrong_content"),
             ("repack failed, the original changed: x", "repack_source_changed"), ("repack failed: x", "repack_failed"),
             ("skipped, not matroska, hardlinked: x", "repack_hardlinked"), ("skipped, not matroska, no item id: x", "not_matroska"),
             ("dropped, the file is gone", "file_gone"), ("dropped: x", "dropped"), ("source checked", "other")]
    for text, code in cases + [("edited", None)]:
        hook.decision(dict(app="radarr", source="hook", result=text, **({"outcome": code} if code else {})), time.time())
    lines = log_lines(env)
    assert [(r["result"], r["outcome"]) for r in lines] == cases + [("edited", "other")]
    assert all(list(r)[-3:] == ["host", "outcome", "took"] for r in lines), lines
    assert hook.logfmt([("a", "x y"), ("b", ""), ("c", 'q"'), ("d", 3)]) == 'a="x y" b="" c="q\\"" d=3'


@pytest.mark.parametrize("case, code", [("hardlink", "repack_hardlinked"), ("cap", "repack_too_big"), ("space", "repack_low_space"),
                                        ("no item id", "not_matroska"), ("name taken", "repack_name_taken"), ("clean", None)])
def test_a_conversion_skip_names_the_code_of_its_reason(env, monkeypatch, settings, tmp_path, case, code):
    """Each reason of convert_skip() comes with its own code, the one the decision line and convert-<app>.txt hold."""
    path = tmp_path / "Film.mp4"
    path.write_bytes(b"x" * 10)
    if case == "hardlink":
        os.link(path, tmp_path / "download.mp4")
    elif case == "cap":
        settings(repack_max=9)
    elif case == "space":
        monkeypatch.setattr(hook.os, "statvfs", lambda p: types.SimpleNamespace(f_bavail=19, f_frsize=1))
    elif case == "name taken":
        (tmp_path / "Film.mkv").write_bytes(b"")
    ids = {} if case == "no item id" else {"app_id": 7}
    got = hook.convert_skip("radarr", str(path), str(tmp_path / "Film.mkv"), os.stat(path), ids)
    assert (got and got[0]) == code, got


# --- metadata checks, language detection, the Zabbix status and the hunter ------------------------------

TMDB = {"original": "eng", "spoken": ["eng"], "runtime": 120, "special": False, "unmapped": [], "source": "tmdb movie 90001",
        "title": "Film A", "year": 1979}
BARE_ENG = {"container": {"properties": {"duration": 7200 * 10**9}},   # the app says Spanish, the only audio is a bare eng tag
            "tracks": [mk("video", "und", 1, True, pixel_dimensions="1920x1080"), mk("audio", "eng", 2, True, audio_channels=6),
                       mk("subtitles", "eng", 3, False, track_name="English")]}
WRONG_RELEASE = "Film.C.2017.1080p.AMZN.WEB-DL.DDP5.1.x264-GRP"   # another film, with Portuguese audio and another year


def lid(env, monkeypatch, tmp_path, answer):
    """arr_lid.py through the hook's real call site. answer(argv) gives the language heard, None, or an exception to raise.
    env["lid"] records (argv, {"timeout": seconds}) per run."""
    lid_dir = tmp_path / "lid"
    lid_dir.mkdir(exist_ok=True)
    (lid_dir / "ready").write_text("")   # the install writes it last
    py = str(lid_dir / "venv" / "bin" / "python")
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, lid_dir=str(lid_dir)))
    real = hook.subprocess.Popen

    class Run:
        pid = 0
        def __init__(self, argv, **kw):
            self.argv, self.kw = argv, kw
            assert kw.get("start_new_session")   # its own process group, so a timeout kills every ffmpeg it started
        def communicate(self, timeout=None):
            env.setdefault("lid", []).append((self.argv, {"timeout": timeout}))
            got = answer(self.argv)
            if isinstance(got, BaseException):
                raise got
            out = {"lang": got, "prob": 0.95 if got else 0.0, "why": None if got else "1 of 3 samples name a language clearly",
                   "cached": False, "took": 20.0}
            return json.dumps(out) + "\n", None
        def poll(self):
            return 0
    monkeypatch.setattr(hook.subprocess, "Popen", lambda argv, **kw: Run(argv, **kw) if argv[0] == py else real(argv, **kw))


def decided(env):
    """The first decision line. A decision line has the schema, see report.render(). An interim line has an outcome too."""
    return [r for r in log_lines(env) if "schema" in r][0]


def test_language_detection_decides_an_undecided_file_and_carries_its_cache(env, monkeypatch, tmp_path):
    arr_lid = hook.arr_lid
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Spanish"}
    env["probe"] = copy.deepcopy(BARE_ENG)
    lid(env, monkeypatch, tmp_path, lambda argv: "spa")
    cache = os.path.join(hook.CFG.state_dir, "lid.sqlite")
    arr_lid.cache_put(cache, env["path"], 0, "small@536b066", 7200, [{"at": 1800}], os.stat(env["path"]))
    real = hook.subprocess.run
    def touch(argv, **kw):   # mkvpropedit rewrites the header, so the mtime moves
        r = real(argv, **kw)
        if argv[0] == "mkvpropedit":
            os.utime(env["path"], ns=(time.time_ns(), os.stat(env["path"]).st_mtime_ns + 10**9))
        return r
    monkeypatch.setattr(hook.subprocess, "run", touch)
    hook.main([])
    (argv, kw), = env["lid"]
    assert argv[2:5] == [env["path"], "0", "7200.0"] and argv[-3:] == ["--expect", "eng", "spa"] and kw["timeout"] <= hook.LID_TIMEOUT
    rec = decided(env)
    # heard Spanish and the app's original agree, so a1 is tagged es too (docs/design.md, "Language tags")
    assert rec["outcome"] == "edited" and rec["edits"] == [["track:=3", 1, 0], ["track:=2", "es", "eng", "language"],
                                                          ["track:=2", "es", None, "language-ietf"]], rec["edits"]
    assert "heard_language" in rec["reasons"] and "language_tag_corrected" in rec["reasons"]
    assert rec["heard"]["a1"]["lang"] == "spa" and rec["tracks"][0]["lang"] == "spa" and rec["tracks"][0]["heard"] == "spa"
    assert rec["recheck"] == {"edits": 0, "undecided": None, "invariants": []}   # the re-plan after the edit hears the same
    assert arr_lid.cache_get(cache, env["path"], 0, "small@536b066", 7200) == [{"at": 1800}]   # carried to the new mtime


# --- language tags (docs/design.md, "Language tags") ----------------------------------------------------------

def tracks(*ts):   # (type, legacy tag, BCP 47 tag or None, uid, default, more properties) in mkvmerge -J shape, 2 hours long
    return {"container": {"properties": {"duration": 7200 * 10**9}},
            "tracks": [mk(typ, lang, uid, dflt, **({"language_ietf": ietf} if ietf else {}), **kw) for typ, lang, ietf, uid, dflt, kw in ts]}


UND_AUDIO = tracks(("video", "eng", "en", 1, True, {}), ("audio", "und", None, 2, True, {"audio_channels": 6}),
                   ("subtitles", "eng", None, 3, True, {"track_name": "English"}))   # one und DTS track
UND_BESIDE_ENG = tracks(("video", "eng", None, 1, True, {}), ("audio", "eng", None, 2, True, {"audio_channels": 6}),
                        ("audio", "eng", None, 3, False, {"audio_channels": 2}), ("audio", "und", None, 4, False, {"audio_channels": 2}))   # an und track beside English ones
HAND_TAGS = tracks(("video", "eng", "en", 1, True, {}), ("audio", "eng", "en", 2, True, {"audio_channels": 6}),
                   ("subtitles", "eng", "en", 3, True, {"codec_id": "S_HDMV/PGS"}))   # its PGS track tagged by hand


def test_an_und_track_heard_as_the_original_is_tagged_in_the_flag_edit(env, monkeypatch, tmp_path):
    env["probe"] = copy.deepcopy(UND_AUDIO)
    lid(env, monkeypatch, tmp_path, lambda argv: "eng")
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited" and rec["edits"] == [["track:=3", 0, 1], ["track:=2", "en", "und", "language"],
                                                          ["track:=2", "en", None, "language-ietf"]], rec
    assert rec["edit_rules"] == ["English subtitle off"] + ["language tag set"] * 2 and "language_tag_set" in rec["reasons"]
    assert env["mkvpropedit"] == [["--edit", "track:=3", "--set", "flag-default=0", "--edit", "track:=2", "--set", "language=en",
                                   "--edit", "track:=2", "--set", "language-ietf=en"]]   # one call for the flags and the tags
    editing = [r for r in log_lines(env) if r.get("result") == "editing"][0]   # the undo record, before the file changes
    assert editing["undo"][2:] == ["--edit", "track:=3", "--set", "flag-default=1", "--edit", "track:=2", "--set", "language=und",
                                   "--edit", "track:=2", "--delete", "language-ietf"]   # the track had no BCP 47 tag
    p = env["files"][env["path"]]["tracks"][1]["properties"]
    assert (p["language"], p["language_ietf"]) == ("eng", "en") and rec["recheck"] == {"edits": 0, "undecided": None, "invariants": []}


@pytest.mark.parametrize("heard, edits", [("kor", []), ("eng", [["track:=4", "en", "und", "language"], ["track:=4", "en", None, "language-ietf"]])])
def test_an_und_track_beside_english_is_heard_alone_and_needs_a_second_signal(env, monkeypatch, tmp_path, heard, edits):
    env["probe"] = copy.deepcopy(UND_BESIDE_ENG)
    lid(env, monkeypatch, tmp_path, lambda argv: heard)
    hook.main([])
    (argv, _), = env["lid"]   # the decision plays a1 without a doubt, so only the und track a3 is heard, ffmpeg audio index 2
    assert argv[3] == "2" and argv[-3:] == ["--expect", "und", "eng"]
    rec = decided(env)
    assert rec.get("edits", []) == edits and rec["tracks"][2]["lang"] == ("und" if heard == "kor" else "eng"), rec
    assert heard == "eng" or ("a3 keeps und: kor (heard kor)" in rec["notes"] and "language_tag_kept" in rec["reasons"])


def test_mkvpropedit_writes_both_tags_and_the_undo_brings_the_old_ones_back(tmp_path, monkeypatch, settings):
    """A real file: a1 und with no BCP 47 tag, a2 eng with en-US and titled Spanish, as --set language-ietf leaves a hand edit."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge") and shutil.which("mkvpropedit")):
        pytest.skip("needs ffmpeg and mkvtoolnix")
    src, path = tmp_path / "src.mkv", tmp_path / "file.mkv"
    REAL_RUN(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
              "-map", "0", "-map", "1", "-c:a", "aac", str(src)], check=True)
    REAL_RUN(["mkvmerge", "-q", "--disable-language-ietf", "-o", str(path), "--language", "0:und", "--language", "1:eng", "--track-name", "1:Spanish",
              str(src)], check=True)
    REAL_RUN(["mkvpropedit", "-q", str(path), "--edit", "track:a2", "--set", "language-ietf=en-US"], check=True)
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    hook.LANGS[:] = []   # the real table from the real mkvmerge
    try:
        tags = lambda: [tuple(t["properties"].get(k) for k in ("language", "language_ietf")) for t in REAL_MKVMERGE(str(path))["tracks"]]
        j = REAL_MKVMERGE(str(path))
        assert tags() == [("und", None), ("eng", "en-US")]
        plan = hook.arr_decide.retag(j, {"a1": "eng", "a2": "spa"}, {"eng", "spa"}, hook.langs())   # heard, the title, the item's languages
        rec = hook.edit({"path": str(path)}, j, plan["edits"], True)
        assert rec["outcome"] == "edited" and tags() == [("eng", "en"), ("spa", "es")], (rec, tags())
        REAL_RUN(rec["undo"], check=True)
        assert tags() == [("und", None), ("eng", "en-US")]   # exact: --delete language-ietf for the track that had none
    finally:
        hook.LANGS[:] = [hook.arr_decide.language_table(LANGUAGES)]


TITLED_JAPANESE = tracks(("video", "eng", None, 1, True, {}), ("audio", "eng", "en", 2, True, {"audio_channels": 6, "track_name": "Japanese"}),
                         ("audio", "eng", None, 3, False, {"audio_channels": 6, "track_name": "English"}))   # not a kids title here


@pytest.mark.parametrize("heard, edits", [("jpn", [["track:=2", "ja", "eng", "language"]]), ("eng", [])])
def test_a_tagged_track_changes_only_when_the_heard_language_agrees(env, monkeypatch, tmp_path, heard, edits):
    """The title "Japanese" and the app's Japanese are metadata. The decision has no doubt, so only the track they
    question is heard, and the tag changes only when the heard language agrees."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Japanese"}
    env["probe"] = copy.deepcopy(TITLED_JAPANESE)
    lid(env, monkeypatch, tmp_path, lambda argv: heard)
    hook.main([])
    (argv, _), = env["lid"]
    assert argv[3] == "0" and argv[-4:] == ["--expect", "eng", "jap", "jpn"]   # a1 alone, because the tag and the title of a2 agree
    rec = decided(env)
    assert rec.get("edits", []) == edits, rec
    assert heard == "jpn" or 'a1 keeps eng/en: eng (tagged eng, heard eng); jpn (the title "Japanese")' in rec["notes"], rec["notes"]


def test_the_limit_on_hearing_starts_after_the_wait_for_the_model(tmp_path, monkeypatch):
    """Another hearing holds the host's model for 1.5 s. A run with a 1-second limit waits for the turn and then hears,
    where it used to spend its second waiting on arr_lid.py's model lock and get no answer."""
    py = fake_lid(tmp_path, monkeypatch, 0.3)
    order = []
    def other():
        with hook.LID_ONE:   # another backfill worker, in the middle of its hearing
            turn = hook.lid_turn(float("inf")); order.append("other"); time.sleep(1.5); turn.close()
    t = threading.Thread(target=other); t.start(); time.sleep(0.2)
    got = hook.lid_run("/m/x.mkv", 0, UND_AUDIO, ["und", "eng"], 1)
    t.join()
    assert got["lang"] == "eng" and got["waited"] >= 1 and order == ["other"], got
    assert py.exists()


def test_a_hook_hearing_waits_for_at_most_one_backfill_hearing(tmp_path, monkeypatch):
    """Three backfill workers need the model, 0.8 s each. A hook job asks while the first one hears. The backfill sends
    one worker at a time, and the gate goes to whoever waits first, so the hook job hears second."""
    fake_lid(tmp_path, monkeypatch, 0.8)
    got, order = {}, []
    real = hook.lid_cli
    monkeypatch.setattr(hook, "lid_cli", lambda path, *a: order.append(path) or real(path, *a))
    backfill = [threading.Thread(target=lambda i=i: got.setdefault(i, hook.lid_run(f"/m/b{i}.mkv", 0, UND_AUDIO, ["und"], 30))) for i in range(3)]
    for b in backfill: b.start()
    time.sleep(0.3)
    started = time.monotonic()
    turn = hook.lid_turn(started + 30)   # the hook job, another process in life, so it never queues at the backfill's LID_ONE
    order.append("hook"); waited = time.monotonic() - started; turn.close()
    for b in backfill: b.join()
    assert waited < 1.2 and order.index("hook") == 1, (waited, order)
    assert [got[i]["lang"] for i in range(3)] == ["eng"] * 3


def test_an_import_edit_waits_for_the_files_in_flight_never_for_queued_hearings(env, monkeypatch, tmp_path):
    """Six dry-run workers each need a hearing of 0.6 s, and the host has one model. A worker drops the file lock while it
    waits for the model and hears, so an import's edit that asks a second in waits for no hearing.
    Before, the workers queued with the lock held shared, and the edit waited for the whole queue."""
    fake_lid(tmp_path, monkeypatch, 0.6)
    env["probe"] = copy.deepcopy(UND_AUDIO)   # one und track a file, so every file is heard
    backfill_movies(env, tmp_path, 6)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    run = threading.Thread(target=hook.main, args=(["--backfill", "radarr", "--workers", "6"],))
    run.start()
    threading.Event().wait(1.0)   # the first hearing is done, the other workers wait for the model
    t0 = time.perf_counter()
    with hook.locked():   # the import's exclusive step
        waited = time.perf_counter() - t0
    run.join()
    recs = [r for r in log_lines(env) if r.get("outcome")]
    assert waited < 0.5, waited
    assert len(recs) == 6 and all(["track:=2", "en", "und", "language"] in r["edits"] for r in recs), recs


def test_a_file_that_changed_during_its_hearing_is_planned_again(env, monkeypatch, tmp_path):
    env["probe"] = copy.deepcopy(UND_AUDIO)
    movies = backfill_movies(env, tmp_path, 1)
    path = movies[0]["movieFile"]["path"]
    def answer(argv):   # the app upgrades the file while the first hearing runs
        if len(env["lid"]) == 1:
            os.utime(path, ns=(time.time_ns(), os.stat(path).st_mtime_ns + 10**9))
        return "eng"
    lid(env, monkeypatch, tmp_path, answer)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr"])
    recs = [r for r in log_lines(env) if r.get("outcome")]
    assert len(env["lid"]) == 2 and [r["outcome"] for r in recs] == ["dry_run"], recs   # heard again, with the lock exclusive
    assert ["track:=2", "en", "und", "language"] in recs[0]["edits"]


def test_a_replan_names_the_step_the_file_changed_in(env):
    """The reasons of Replan, which a hook job's warning line quotes, see run_job(): the checks of a backfill's
    upgrade(), as of exclusive(), and the hearing of reshared()."""
    path, reasons = env["path"], []
    st = os.stat(path)
    with open(os.path.join(hook.CFG.state_dir, "lock"), "w") as lock:
        for change in (lambda: os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9)), lambda: os.remove(path)):
            change()
            for step in (hook.upgrade, hook.reshared):
                with pytest.raises(hook.Replan) as ex:
                    step(lock, path, st)
                reasons.append(str(ex.value))
    assert reasons == ["the file changed since the checks", "the file changed during the hearing", "the file is gone since the checks",
                       "the file is gone since the hearing"]


def fake_lid(tmp_path, monkeypatch, secs):
    """arr_lid.py as a script that holds the model lock for secs and answers eng. Returns its path."""
    lid_dir = tmp_path / "lid"
    (lid_dir / "venv" / "bin").mkdir(parents=True)
    (lid_dir / "ready").write_text("")
    py = lid_dir / "venv" / "bin" / "python"
    py.write_text(f"#!{sys.executable}\nimport fcntl, json, sys, time\ncache = sys.argv[sys.argv.index('--cache') + 1]\n"
                  f"with open(cache + '.lock', 'w') as f:\n    fcntl.flock(f, fcntl.LOCK_EX); time.sleep({secs})\n"
                  "print(json.dumps({'lang': 'eng', 'prob': 0.95, 'why': None, 'cached': False, 'took': 0.1}))\n")
    py.chmod(0o755)
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, lid_dir=str(lid_dir), state_dir=str(tmp_path)))
    return py


def backfill_movies(env, tmp_path, n):
    """n Film A copies for a backfill, each with a Portuguese default and an English track."""
    movies = []
    for i in range(n):
        path = tmp_path / "media" / f"Film A {i}.mkv"
        path.write_bytes(b"x")
        movies.append(dict(env["movies"]["movie/7"], id=i, title=f"Film A {i}", movieFile={"id": i, "path": str(path), "mediaInfo": {}}))
    env["movies"]["movie"] = movies
    return movies


def test_a_dry_run_reads_files_side_by_side_under_the_shared_lock(env, monkeypatch, tmp_path, capsys):
    backfill_movies(env, tmp_path, 6)
    ops, inside, peak = [], [0], [0]
    real_flock = hook.fcntl.flock
    def flock(f, op):
        if f.name.endswith("/lock") and op != hook.fcntl.LOCK_UN: ops.append(op)
        real_flock(f, op)
    real_process = hook.process
    def process(*args, **kw):   # counts the files in flight
        inside[0] += 1; peak[0] = max(peak[0], inside[0])
        try:
            threading.Event().wait(0.05)   # long enough for the other workers to start
            return real_process(*args, **kw)
        finally:
            inside[0] -= 1
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    monkeypatch.setattr(hook, "process", process)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--workers", "3"])
    assert "6 mkv files, each read for its own tracks, dry run, 3 at a time" in capsys.readouterr().out
    assert [r["label"] for r in log_lines(env) if r.get("outcome")] == [f"Film A {i} (1979)" for i in range(6)]   # in file order
    assert set(ops) == {hook.fcntl.LOCK_SH} and peak[0] > 1 and env["mkvpropedit"] == []
    with pytest.raises(SystemExit):
        hook.main(["--backfill", "radarr", "--apply", "--workers", "2"])   # an apply edits one file at a time


def test_an_apply_takes_the_lock_exclusive_only_for_the_edit_and_replans_a_changed_file(env, monkeypatch, tmp_path):
    backfill_movies(env, tmp_path, 1)
    path = env["movies"]["movie"][0]["movieFile"]["path"]
    ops, touched = [], []
    real_flock = hook.fcntl.flock
    def flock(f, op):
        if f.name.endswith("/lock"): ops.append({hook.fcntl.LOCK_SH: "shared", hook.fcntl.LOCK_EX: "exclusive", hook.fcntl.LOCK_UN: "off"}[op])
        real_flock(f, op)
    def tmdb(app, ids, **k):   # the app replaces the file while the first pass checks it, once
        if not touched:
            touched.append(1); os.utime(path, ns=(time.time_ns(), os.stat(path).st_mtime_ns + 10**9))
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    monkeypatch.setattr(hook.arr_meta, "expected_languages", tmdb)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--apply"])
    recs = [r for r in log_lines(env) if r.get("outcome")]
    assert [r["outcome"] for r in recs] == ["edited"] and len(env["mkvpropedit"]) == 1
    # checks shared, the lock traded for the edit, the file changed, so the file runs again with the lock exclusive
    assert ops[:4] == ["shared", "off", "exclusive", "exclusive"], ops


def test_backfill_selects_files_by_their_own_tracks(env, monkeypatch, tmp_path, capsys):
    # Radarr's stored mediaInfo said one audio track and no subtitle for a film, and the backfill never read it
    files = {"Film E": (HAND_TAGS, {"audioStreamCount": 1, "subtitles": ""}),
             "One Track": (NO_ENGLISH, {"audioStreamCount": 2, "subtitles": "English"}),   # stale the other way
             "Untagged": (tracks(("audio", "und", None, 2, True, {})), {"audioStreamCount": 1, "subtitles": ""})}
    movies = []
    for i, (name, (probe, mi)) in enumerate(files.items()):
        path = tmp_path / "media" / f"{name}.mkv"
        path.write_bytes(b"x")
        env["files"][str(path)] = copy.deepcopy(probe)
        movies.append(dict(env["movies"]["movie/7"], id=i, title=name, movieFile={"id": i, "path": str(path), "mediaInfo": mi}))
    env["movies"]["movie"] = movies
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr"])
    assert '"not_selected": 1' in capsys.readouterr().out
    recs = [r for r in log_lines(env) if r.get("outcome")]
    assert [r["label"] for r in recs] == ["Film E (1979)", "Untagged (1979)"]
    assert recs[0]["edits"] == [["track:=3", 0, 1]] and recs[0]["outcome"] == "dry_run"   # the subtitle goes off, the hand tags stay


def test_language_detection_unavailable_or_slow_is_no_answer(env, monkeypatch, tmp_path):
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Spanish"}
    env["probe"] = copy.deepcopy(BARE_ENG)
    hook.main([])   # the fixture's venv does not exist
    rec = decided(env)
    assert rec["outcome"] == "undecided" and rec["heard"]["a1"] == {"lang": None, "prob": None, "why": "language detection is not installed",
                                                                   "cached": None, "took": None}
    lid(env, monkeypatch, tmp_path, lambda argv: subprocess.TimeoutExpired(argv, 120))
    hook.main([])
    rec = [r for r in log_lines(env) if r.get("outcome")][-1]
    assert rec["outcome"] == "undecided" and rec["heard"]["a1"]["why"] == "no answer in 120 seconds" and env["mkvpropedit"] == []


def test_the_time_limit_passes_language_detection(env, monkeypatch, tmp_path):
    """The time limit raises OutOfTime, which is no OSError, so nothing between the lock and mkvpropedit can swallow it."""
    with pytest.raises(hook.arr_meta.OutOfTime, match="^stopped after 300 seconds$"):
        hook.DEADLINE.start(hook.BUDGET)
        env["clock"][0] += hook.BUDGET
        hook.DEADLINE.check()
    assert not issubclass(hook.arr_meta.OutOfTime, OSError) and hook.DEADLINE.end is None   # it ends the limit once raised
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Spanish"}
    env["probe"] = copy.deepcopy(BARE_ENG)
    lid(env, monkeypatch, tmp_path, lambda argv: hook.arr_meta.OutOfTime("stopped after 300 seconds"))
    hook.main([])
    assert log_lines(env)[-1]["result"] == "error: OutOfTime: stopped after 300 seconds" and env["mkvpropedit"] == []


def test_tmdb_languages_decide_the_language_alert(env):
    env["probe"] = copy.deepcopy(NO_ENGLISH)
    env["tmdb"] = dict(TMDB, original="por", spoken=["por"])   # Radarr's original language is wrong, the file is right
    hook.main([])
    rec = decided(env)
    assert rec["alert_kinds"] == [] and rec["tmdb"] == "found" and rec["evidence"]["signals"][0]["verdict"] == "ok"


def test_a_header_no_second_source_confirms_still_alerts(env):
    env["probe"]["container"]["properties"]["duration"] = 48213 * 10**9   # 13:23:33, far past the real end
    env["last_packet"] = 6873.418
    hook.main([])
    rec = decided(env)
    assert rec["trusted"]["trust"] == "conflict"   # no runtime verdict, so the old runtime alert is gone
    assert rec["findings"] == [{"kind": "duration", "why": "The file says it runs 13:23:33, but the video and audio stop at 1:54:33. The runtime "
                                                           "check was skipped."}]


def wrong_film(env, monkeypatch):
    """Another film with Portuguese audio, a 2017 release name and a grab record. Two points, so a re-grab."""
    env["probe"] = copy.deepcopy(NO_ENGLISH)
    env["tmdb"] = dict(TMDB)
    grabbed(env, monkeypatch)
    monkeypatch.setenv("radarr_moviefile_scenename", WRONG_RELEASE)


def test_wrong_content_only_says_it_would_regrab_while_switched_off(env, monkeypatch):
    wrong_film(env, monkeypatch)
    hook.main([])
    rec = decided(env)
    assert env["writes"] == [] and rec["outcome"] == "would_regrab" and rec["edit_result"] == "no change" and rec["evidence"]["points"] == 2
    assert rec["findings"][0] == {"kind": "content", "signals": ["the audio is Portuguese, but it should be English",
                                                                 "the release name says 2017, but the listed year is 1979"],
                                  "scored": ["language", "year"], "points": 2, "action": {"code": "would_regrab", "kind": "content"}}
    assert rec["alert_kinds"] == ["content", "language"] and rec["alert_result"] == ["sent", "log only"]   # the content alert names the language
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1 and not hook.store.items("regrabs")


def test_wrong_content_regrabs_the_download_when_switched_on(env, monkeypatch, settings):
    wrong_film(env, monkeypatch)
    settings(regrab={"audio", "video", "content"})
    hook.main([])
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "history/failed/2101", None)]
    rec = decided(env)
    assert rec["outcome"] == "wrong_content" and regrabs_counted("radarr") == 1
    (f,) = [f for f in rec["findings"] if f["kind"] == "content"]
    assert f["action"] == {"code": "regrabbed", "name": "Radarr", "kind": "content", "n": 1, "came": [], "linked": True, "own_copy": True,
                           "others": 0, "stayed": None}
    assert rec["alert_result"] == ["log only", "log only"] and not [b for m, u, b in env["http"] if m == "POST"]   # the re-grab and its language
    hook.main([])   # the app deletes the file, the fake does not: a later job of the unit is skipped
    assert (log_lines(env)[-1]["outcome"], log_lines(env)[-1]["fault"]) == ("deleted_with_download", "content") and len(env["writes"]) == 3
    assert hook.content_probe("radarr", "7", "English", False, "", {}, None)(env["path"], {"eps": []}) == (None, {"skipped": "another film"})


def test_wrong_content_keeps_the_file_when_a_second_check_disagrees(env, monkeypatch, settings):
    wrong_film(env, monkeypatch)
    settings(regrab={"audio", "video", "content"})
    answers = iter([dict(TMDB), dict(TMDB, original="por", spoken=["por"])])   # the second check reads Portuguese as right
    monkeypatch.setattr(hook.arr_meta, "expected_languages", lambda *a, **k: next(answers))
    hook.main([])
    assert env["writes"] == [] and decided(env)["findings"][0]["action"] == {"code": "unconfirmed"}


def status_file(env):
    with open(os.path.join(hook.CFG.state_dir, "status.json")) as f:
        return json.load(f)


def test_status_records_the_policy_and_tmdb_only_after_a_live_answer(env, monkeypatch):
    hook.main([])   # no TMDB answer at all: the policy is recorded, TMDB is not
    data = status_file(env)
    assert data["checks"]["policy"]["status"] == "ok" and data["checks"]["tmdb"]["status"] == "unknown"
    assert data["last_hook_run"] == int(env["clock"][0]) - hook.PLEX_QUIET   # the analyze waited PLEX_QUIET after the job
    def rejected(*a, **k):
        hook.arr_meta.DOWN.update(until=time.time() + 600, code="tmdb_token_rejected", why="HTTPError: HTTP Error 401: Unauthorized",
                                  answered=time.time())
        return None
    monkeypatch.setattr(hook.arr_meta, "expected_languages", rejected)
    env["files"].clear(); env["probe"] = copy.deepcopy(PORTUGUESE_DEFAULT)
    hook.main([]); env["files"].clear(); hook.main([])
    assert status_file(env)["checks"]["tmdb"]["status"] == "token_rejected"
    posts = [b["embeds"][0]["title"] for m, u, b in env["http"] if m == "POST"]
    assert posts == ["TMDB key not working"]   # once a day, not once per file
    env["clock"][0] += 1000
    before = status_file(env)
    hook.main(["--selftest"])   # a dry run: no ARR_MEDIA_GUARD_RECORD, so nothing is written
    assert status_file(env) == before
    monkeypatch.setenv("ARR_MEDIA_GUARD_RECORD", "1")   # an install: it records the policy and leaves last_hook_run
    hook.main(["--selftest"])
    data = status_file(env)
    assert data["last_hook_run"] == int(env["clock"][0]) - 1000 - hook.PLEX_QUIET and data["checks"]["policy"]["checked"] == int(env["clock"][0])


def test_subhunt_names_its_own_command(env, capsys):
    """The subtitle hunter is a command of its own, arr-media-guard-subhunt. --subhunt names it and exits 2."""
    with pytest.raises(SystemExit) as ex:
        hook.main(["--subhunt", "radarr", "--ids", "4041"])
    assert ex.value.code == 2 and "arr-media-guard-subhunt" in capsys.readouterr().err


def test_only_undecided_replans_the_undecided_files_of_a_plan(env, monkeypatch, tmp_path):
    movies = [{"id": i, "title": f"Movie {i}", "year": 2000, "originalLanguage": {"name": "English"}, "runtime": 120,
               "movieFile": {"path": f"/m/Movie {i}.mkv", "mediaInfo": {"audioStreamCount": 2}}} for i in (1, 2, 3)]
    rows = [plan_line("Movie 1", "undecided", undecided="the app says Spanish"), plan_line("Movie 2", "x", [["track:=2", 0, 1]]),
            plan_line("Movie 3", "no change")]
    plans = tmp_path / "plans.jsonl"
    plans.write_text("".join(json.dumps(r) + "\n" for r in rows))
    seen = []
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    monkeypatch.setattr(hook, "process", lambda ctx: seen.append(ctx.path) or {"outcome": "no_change", "result": "no change"})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--plan-from", str(plans), "--only-undecided"])
    assert seen == ["/m/Movie 1.mkv"]
    with pytest.raises(SystemExit):
        hook.main(["--backfill", "radarr", "--only-undecided"])


def test_own_version_leaves_out_a_module_that_is_not_deployed(monkeypatch):
    v = hook.own_version()
    monkeypatch.setattr(hook, "LIB", hook.LIB + ("arr_gone.py",))
    assert hook.own_version() == v and len(v) == 12
    monkeypatch.setattr(hook, "LIB", ("arr_decide.py",))
    assert hook.own_version() != v   # every deployed module counts


# --- re-grab units, wrong content and language detection -----------------------------------

NOT_MATROSKA = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "not_matroska.json")))
UNTAGGED = {"container": {"properties": {"duration": 7200 * 10**9}},
            "tracks": [mk("video", "und", 1, True, pixel_dimensions="1920x1080"), mk("audio", "und", 2, True, audio_channels=6)]}


def test_a_file_of_another_series_stays_out_of_the_unit(env, monkeypatch, tmp_path):
    """A manual import mapped file 204 of the pack to another series. It is not judged or deleted with this unit."""
    paths = pack(env, monkeypatch, tmp_path, broken=(1, 4), other={104: 6})
    import_event(monkeypatch, paths, 201)
    assert [p for m, p, b in env["writes"] if m == "DELETE"] == ["episodefile/201"]
    assert [r["note"] for r in log_lines(env) if r.get("result") == "warning"] == [
        "file 204 left out of the re-grab unit: episode 104 is of series 6, the job is of series 5"]
    probe = hook.content_probe("sonarr", "5", "English", False, "", {"listed": [22]}, None)
    assert probe(paths[204], {"eps": [{"seriesId": 6, "runtime": 22}]}) == (None, {"skipped": "another series"})


@pytest.mark.parametrize("case", ["no_grab", "capped", "unconfirmed"])
def test_a_wrong_content_verdict_names_what_happened(env, monkeypatch, case):
    wrong_film(env, monkeypatch)
    if case == "no_grab":
        env["movies"]["history?downloadId=a1b2c3d4&pageSize=1000"] = {"records": []}
    elif case == "capped":
        hook.store.put("regrabs", "radarr", [env["clock"][0]] * hook.CFG.regrab_cap)
    else:
        answers = iter([dict(TMDB), dict(TMDB, original="por", spoken=["por"])])
        monkeypatch.setattr(hook.arr_meta, "expected_languages", lambda *a, **k: next(answers))
    hook.main([])
    rec = decided(env)
    outcome = {"no_grab": "regrab_no_grab", "capped": "regrab_capped", "unconfirmed": "wrong_content_unconfirmed"}[case]
    assert (rec["outcome"], rec["regrab"], rec["edit_result"], env["writes"]) == (outcome, case, "no change", [])
    act = {"no_grab": {"code": "no_grab", "name": "Radarr"}, "capped": {"code": "capped", "cap": hook.CFG.regrab_cap},
           "unconfirmed": {"code": "unconfirmed"}}[case]
    assert (rec["findings"][0]["kind"], rec["findings"][0]["action"]) == ("content", act)
    assert rec["alert_result"] == ["sent", "log only"] and len([b for m, u, b in env["http"] if m == "POST"]) == 1   # it names the language


@pytest.mark.parametrize("again, outcome", [("por", "would_regrab"), (None, "wrong_content_unconfirmed")])
def test_the_second_check_hears_again_and_asks_tmdb_again(env, monkeypatch, tmp_path, again, outcome):
    """The first check heard Portuguese on an untagged track. The second hears past the cache and asks TMDB past its
    cache. When it hears nothing, the language point does not count, and one point is no re-grab."""
    wrong_film(env, monkeypatch)
    env["probe"] = copy.deepcopy(UNTAGGED)
    answers = iter(["por", again])
    lid(env, monkeypatch, tmp_path, lambda argv: next(answers))
    caches = []
    monkeypatch.setattr(hook.arr_meta, "expected_languages",
                        lambda app, ids, token=None, cache=None, **k: caches.append(cache) or dict(TMDB))
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == outcome and rec["heard"]["a1"]["lang"] == "por" and env["writes"] == []
    assert ["--fresh" in argv for argv, _ in env["lid"]] == [False, True] and caches == ["tmdb", "tmdb-recheck"]


def test_a_language_the_second_check_does_not_hear_again_never_counts(env, monkeypatch, tmp_path, settings):
    """The first check heard Portuguese on the Portuguese track. The second check hears nothing there, so its
    language points go and the year alone is one point. No re-grab."""
    wrong_film(env, monkeypatch)
    settings(regrab={"audio", "video", "content"})
    answers = iter(["por", None])
    lid(env, monkeypatch, tmp_path, lambda argv: next(answers))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["heard"]["a1"]["lang"], env["writes"]) == ("wrong_content_unconfirmed", "por", [])
    assert ["--fresh" in argv for argv, _ in env["lid"]] == [False, True]


@pytest.mark.parametrize("alarm, asked", [(65.0, None), (100.0, 40.0), (0.0, 120.0)])
def test_language_detection_leaves_the_audio_samples_their_time(env, monkeypatch, tmp_path, alarm, asked):
    """hear() takes at most the job's time left minus LID_RESERVE. With under 10 seconds it skips, and the plan goes on."""
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Spanish"}
    env["probe"] = copy.deepcopy(BARE_ENG)
    lid(env, monkeypatch, tmp_path, lambda argv: "spa")
    monkeypatch.setattr(hook.DEADLINE, "left", lambda: alarm or None)
    hook.main([])
    rec = decided(env)
    if asked is None:
        assert "lid" not in env and rec["heard"]["a1"]["why"] == "no time left of the 5 seconds detection may take"
        assert rec["outcome"] == "undecided" and len(env["ffmpeg"]) == 3   # the audio samples still run
    else:
        assert env["lid"][0][1]["timeout"] == asked and rec["outcome"] == "edited"


def test_a_detection_timeout_kills_the_whole_process_group(tmp_path, monkeypatch, settings):
    lid_dir = tmp_path / "lid"
    (lid_dir / "venv" / "bin").mkdir(parents=True)
    (lid_dir / "ready").write_text("")
    pid_file = tmp_path / "cut.pid"
    py = lid_dir / "venv" / "bin" / "python"   # stands in for arr_lid.py: it starts a long "ffmpeg cut" and waits on it
    py.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > '{pid_file}'\nwait\n")
    py.chmod(0o755)
    settings(lid_dir=str(lid_dir), state_dir=str(tmp_path))
    assert hook.lid_run("/m/x.mkv", 0, PORTUGUESE_DEFAULT, ["eng"], 1) == {"why": "no answer in 1 seconds"}
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] in "ZX":
                    break
        except (FileNotFoundError, ProcessLookupError):   # gone, and a read of a vanishing entry raises ESRCH
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"the cut {pid} outlived the timeout")


def test_a_half_built_install_is_not_installed(env, monkeypatch, settings, tmp_path):
    (tmp_path / "lid" / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "lid" / "venv" / "bin" / "python").write_text("")   # pip failed after the venv: no ready marker
    settings(lid_dir=str(tmp_path / "lid"))
    assert hook.lid_run("/m/x.mkv", 0, PORTUGUESE_DEFAULT, ["eng"], 120) == {"why": "language detection is not installed"}


# --- the repack of a .mkv file that is not Matroska ---------------------------------------------------
# Some files are MP4 under a .mkv name. The hook remuxes such a file into Matroska, verifies the copy,
# renames it over the original, asks the app to rescan the item, and decides the new file in the same job.

NEW_MKV = {"container": {"type": "Matroska", "properties": {"duration": 7200 * 10**9, "writing_application": "mkvmerge v92.0"}},
           "tracks": [mk("video", "und", 21, True), mk("audio", "eng", 22, True, audio_channels=6), mk("subtitles", "eng", 23, True)]}


def mp4_named_mkv(env):
    env["probe"] = copy.deepcopy(NOT_MATROSKA["probe"])
    os.chmod(env["path"], 0o640)
    return os.path.dirname(env["path"])


def test_an_mp4_named_mkv_is_repacked_rescanned_and_decided(env, monkeypatch):
    folder = mp4_named_mkv(env)
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: env["writes"].append((method, p, body)) or env["events"].append("rescan"))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["reasons"][0], rec["container"]) == ("edited", "repacked", NOT_MATROSKA["container_type"]), rec["result"]
    assert [r.get("outcome", r["result"]) for r in log_lines(env)][:3] == ["repacked", "editing", "edited"]   # the repack record comes first
    assert rec["repack"]["old_size"] == 1000 and rec["repack"]["new_size"] == 1004 and rec["repack"]["rescan"] == "sent"
    assert [t[0] for t in rec["repack"]["tracks"]] == ["video", "audio", "subtitles"] and rec["repack"]["new_tracks"][1][2] == "eng"
    tmp = os.path.join(folder, hook.CFG.hide_dir, "." + os.path.basename(env["path"]) + ".repack-tmp")   # a hidden folder the apps skip
    assert env["repacks"][0][:10] == ["ionice", "-c3", "nice", "-n", "19", "mkvmerge", "-q", "--disable-lacing", "-o", tmp]
    events = env["events"]
    assert events[events.index("repack") - 1] == "limit off" and f"limit {hook.BUDGET}" in events[events.index("repack"):]
    assert events.index("mkvpropedit") < events.index("rescan")   # the app scans the file after the flag edit, never during it
    assert env["writes"] == [("POST", "command", {"name": "RescanMovie", "movieId": 7})]
    assert open(env["path"], "rb").read(4) == b"MKV!" and os.stat(env["path"]).st_mode & 0o777 == 0o640
    assert os.listdir(folder) == [os.path.basename(env["path"])]
    assert env["mkvpropedit"] == [["--edit", "track:=23", "--set", "flag-default=0"]]   # the new file's English subtitle goes off
    assert analyzes(env) == ["/library/metadata/7101/analyze"] and [u for m, u, b in env["http"] if m == "POST"] == []
    hook.main([])   # the next import of the file finds Matroska and plans nothing more
    assert len(env["repacks"]) == 1 and decided(env) and log_lines(env)[-1]["outcome"] == "no_change"


def test_an_in_place_conversion_lets_the_lock_go_like_a_renamed_one(env):
    """A backfill worker's conversion of a .mkv holding MP4 ends with the file lock free, as one with a new name does.
    process() then takes it exclusive at the gate. A held lock would deadlock a second worker that waits there."""
    mp4_named_mkv(env)
    lock = hook.locked(shared=True)
    assert hook.convert("radarr", env["path"], hook.mkvmerge(env["path"]), os.stat(env["path"]), True, {"app_id": 7}, lock)[0] == "repacked"
    with open(lock.name) as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)   # BlockingIOError while convert() still holds it


def test_a_sonarr_repack_rescans_the_series(env, monkeypatch):
    mp4_named_mkv(env)
    as_sonarr(monkeypatch, env, {"title": "Show F", "originalLanguage": {"name": "English"}, "runtime": 22},
              [{"id": 91, "seasonNumber": 1, "episodeNumber": 5, "runtime": 22}])
    hook.main([])
    assert env["writes"] == [("POST", "command", {"name": "RescanSeries", "seriesId": 5})] and decided(env)["reasons"][0] == "repacked"


@pytest.mark.parametrize("fault, why", [
    ("rc", "mkvmerge exited 2"),   # an error, not a warning
    ("container", "the new file reads as MP4/QuickTime"),
    ("proof", "the packet data of stream audio 1 (aac) differ"),
])
def test_a_repack_that_fails_its_checks_keeps_the_original(env, fault, why):
    folder = mp4_named_mkv(env)
    if fault == "rc":
        env["repack_rc"], env["repack_out"] = 2, "Error: the file could not be read"
    elif fault == "container":
        env["mkv_probe"]["container"]["type"] = "MP4/QuickTime"
    else:
        env["proof"] = ((1, "the packet data of stream audio 1 (aac) differ"), [])
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and why in rec["result"] and rec["reasons"] == ["not_matroska"], rec
    assert open(env["path"], "rb").read() == b"x" * 1000 and os.listdir(folder) == [os.path.basename(env["path"])]
    assert env["mkvpropedit"] == [] and env["writes"] == [] and analyzes(env) == []
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1
    (f,) = rec["findings"]
    assert (f["kind"], f["container"]) == ("repack", "MP4/QuickTime"), f


def test_a_repack_keeps_the_upgrade_the_app_imported_during_the_remux(env):
    """The app imported an upgrade under the same name while mkvmerge ran. The rename would overwrite it, so the temp
    file goes, and nothing posts: the new file gets its own hook job."""
    folder = mp4_named_mkv(env)
    upgrade = env["path"] + ".part"

    def import_upgrade():
        with open(upgrade, "wb") as f:
            f.write(b"u" * 2000)
        os.replace(upgrade, env["path"])
    env["during_repack"] = import_upgrade
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_source_changed" and "the app replaced or renamed the original" in rec["result"], rec
    assert open(env["path"], "rb").read() == b"u" * 2000 and os.listdir(folder) == [os.path.basename(env["path"])]
    assert env["mkvpropedit"] == [] and env["writes"] == [] and [u for m, u, b in env["http"] if m == "POST"] == []


def test_a_long_name_keeps_its_repack_temp_under_255_bytes():
    name = "Ö" * 130 + ".mkv"   # 264 bytes
    tmp = os.path.basename(hook.repack_tmp(os.path.join("/m", name)))
    assert len(os.fsencode(tmp)) == 255 and tmp.startswith(".Ö") and tmp.endswith(".repack-tmp")
    assert hook.repack_tmp("/m/a/b.mkv") == f"/m/a/{hook.CFG.hide_dir}/.b.mkv.repack-tmp"


def test_the_worker_removes_a_stale_repack_temp_file(env):
    """A SIGKILL mid-remux leaves the temp file. The next job in its folder removes it after a day and logs it. A temp
    file two hours old may be one a conversion worker of a backfill still proves, so it stays. Older versions wrote the
    temp file beside the video, so the folder itself is swept too."""
    folder = os.path.dirname(env["path"])
    hidden = os.path.join(folder, hook.CFG.hide_dir)
    os.mkdir(hidden)
    old, new, proof = (os.path.join(hidden, f".{n}.mkv.repack-tmp") for n in ("Old", "Busy", "Proof"))
    older = os.path.join(folder, ".Older.mkv.repack-tmp")
    for f, age in ((old, 86500), (older, 90000), (new, 60), (proof, 7200)):
        open(f, "wb").close()
        os.utime(f, (env["clock"][0] - age,) * 2)
    hook.main([])
    assert sorted(os.listdir(folder)) == sorted([os.path.basename(env["path"]), hook.CFG.hide_dir])
    assert sorted(os.listdir(hidden)) == sorted([os.path.basename(new), os.path.basename(proof)])
    w = [r for r in log_lines(env) if r.get("note", "").startswith("removed a repack temp")]
    assert sorted(r["path"] for r in w) == sorted([old, older]) and {(r["source"], r["result"]) for r in w} == {("hook", "warning")}


INTERRUPT = """
import dataclasses, json, os, sys, time
sys.path.insert(0, sys.argv[1])
import amg
h = amg.load("h")
h.CFG = dataclasses.replace(h.CFG, state_dir=os.path.join(os.path.dirname(os.path.dirname(sys.argv[2])), "state"))   # the work folders go there
os.makedirs(h.CFG.state_dir, exist_ok=True)
h.mkvmerge = lambda p: time.sleep(60)   # the checks never end, so the signal lands inside the conversion
print(h.convert("radarr", sys.argv[2], {"container": {"type": "MP4/QuickTime"}, "tracks": []}, os.stat(sys.argv[2]), True), flush=True)
"""


def written(path):
    """The file at path holds a byte. It may go at any moment, when the process that writes it stops."""
    try:
        return os.stat(path).st_size > 0
    except FileNotFoundError:
        return False


def interrupted(tmp_path, script, source, tool, sig):
    """Run script on a copy of source in a process of its own, send sig while its remux writes the temp file, and check
    that the temp file goes, the original stays and the process still ends. The fake mkvmerge stops mid-write. A real
    mkvmerge, when installed, writes a whole file first."""
    env = dict(os.environ)
    if tool == "fake":
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        (bin_dir / "mkvmerge").write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nprintf partial > "$2"\nexec sleep 60\n')
        (bin_dir / "mkvmerge").chmod(0o755)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    elif not shutil.which("mkvmerge"):
        pytest.skip("needs mkvmerge")
    path = tmp_path / "Movie (2020)" / "Movie (2020).mkv"
    path.parent.mkdir()
    shutil.copy(source, path)
    before, tmp = path.read_bytes(), hook.repack_tmp(str(path))
    p = subprocess.Popen([sys.executable, "-c", script, os.path.dirname(__file__), str(path)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 120   # mkvmerge runs at nice 19 and idle I/O, so a loaded host starts it late
    while not written(tmp) and time.time() < deadline:
        time.sleep(0.005)
    assert os.path.exists(tmp), p.communicate(timeout=5)
    p.send_signal(sig)
    out, err = p.communicate(timeout=120)
    assert p.returncode == (128 + signal.SIGTERM if sig == signal.SIGTERM else -signal.SIGINT), (p.returncode, out, err)
    assert os.listdir(path.parent) == [path.name] and path.read_bytes() == before, err


EDIT_INTERRUPT = """
import dataclasses, json, os, sys
sys.path.insert(0, sys.argv[1])
import amg
h = amg.load("h")
d = os.path.dirname(sys.argv[2])
h.CFG = dataclasses.replace(h.CFG, log=os.path.join(d, "log.jsonl"), state_dir=d)
rec = h.edit({"path": sys.argv[2]}, json.loads(sys.argv[3]), [["track:=2", "spa", "eng", "language"]], True)
print("after the edit", rec["outcome"], flush=True)
"""


def test_a_ctrl_c_during_mkvpropedit_lets_the_write_finish_then_stops(tmp_path):
    """Ctrl+C in a terminal sends SIGINT to the whole foreground process group, mkvpropedit included. no_stop() blocks
    it, and the child inherits the block, so the write finishes. The run stops right after it."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    (bin_dir / "mkvpropedit").write_text(f"#!{sys.executable}\nimport sys, time\n"   # dash clears its signal mask at start, mkvpropedit does not
                                         "open(sys.argv[1], 'ab').write(b'part1')\ntime.sleep(1)\nopen(sys.argv[1], 'ab').write(b' part2')\n")
    (bin_dir / "mkvpropedit").chmod(0o755)
    path = tmp_path / "Movie.mkv"
    path.write_bytes(b"")
    j = {"tracks": [mk("video", "und", 1, True), mk("audio", "eng", 2, True)]}
    p = subprocess.Popen([sys.executable, "-c", EDIT_INTERRUPT, os.path.dirname(__file__), str(path), json.dumps(j)], start_new_session=True,
                         env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 60
    while not written(path) and time.time() < deadline:
        time.sleep(0.005)
    os.killpg(p.pid, signal.SIGINT)   # the terminal's Ctrl+C, to every process of the group
    out, err = p.communicate(timeout=60)
    assert (p.returncode, out) == (-signal.SIGINT, ""), (p.returncode, out, err)
    assert path.read_bytes() == b"part1 part2" and "KeyboardInterrupt" in err
    assert [json.loads(x)["result"] for x in open(tmp_path / "log.jsonl")] == ["editing"]


@pytest.mark.parametrize("tool", ["fake", "mkvmerge"])
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupted_repack_leaves_no_temp_file(media, tmp_path, tool, sig):
    """Ctrl+C in a backfill, or SIGTERM from a stop of radarr.service, during the remux or the checks. The temp file goes,
    the original stays, and the process still ends."""
    interrupted(tmp_path, INTERRUPT, media / "good.mp4", tool, sig)


@pytest.mark.parametrize("case, outcome", [("hardlink", "repack_hardlinked"), ("cap", "repack_too_big"), ("space", "repack_low_space")])
def test_a_repack_is_skipped_on_a_hardlink_the_size_cap_or_low_space(env, monkeypatch, settings, case, outcome):
    mp4_named_mkv(env)
    if case == "hardlink":
        os.link(env["path"], env["path"] + ".download")
    elif case == "cap":
        settings(repack_max=999)
    else:
        monkeypatch.setattr(hook.os, "statvfs", lambda p: type("V", (), {"f_bavail": 1999, "f_frsize": 1})())
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == outcome and env["repacks"] == [] and env["writes"] == [] and env["mkvpropedit"] == [], rec
    assert [u for m, u, b in env["http"] if m == "POST"] == []   # a skip posts nothing


def test_a_conversion_runs_with_no_place_to_keep_an_original(env, monkeypatch, settings):
    """A conversion keeps no original unless forced or a track leaves the file. So convert_skip() leaves out the keep
    check of repack_block(), and a keep folder that cannot be written skips no conversion."""
    mp4_named_mkv(env)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "keepable", lambda p, st: "/m/.kept is not writable")
    assert hook.repack_block(env["path"], os.stat(env["path"])) == ("keep", "the original cannot be kept: /m/.kept is not writable")
    assert hook.convert("radarr", env["path"], hook.mkvmerge(env["path"]), os.stat(env["path"]), True, {"app_id": 7})[0] == "repacked"


def test_a_repack_dry_run_the_apply_and_both_audits(env, monkeypatch, tmp_path, capsys):
    mp4_named_mkv(env)
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 70, "movieId": 7, "path": env["path"],
                                                                              "mediaInfo": {"audioStreamCount": 2}})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    plans = tmp_path / "plans.jsonl"
    hook.main(["--backfill", "radarr", "--plan-out", str(plans)])
    assert decided(env)["outcome"] == "would_repack" and env["repacks"] == []
    hook.main(["--audit", "radarr", "--plan-from", str(plans)])
    assert "1 would be repacked into Matroska." in capsys.readouterr().out
    hook.main(["--backfill", "radarr", "--apply", "--plan-from", str(plans)])   # a would-repack plan is kept for the apply
    assert len(env["repacks"]) == 1 and env["writes"] == [("POST", "command", {"name": "RescanMovie", "movieId": 7})]
    hook.main(["--audit", "radarr", "--since", "24h"])
    out = capsys.readouterr().out
    assert "1 repacked into Matroska." in out and "1 repacked from MP4/QuickTime (Film A (1979))" in out


def test_a_plan_audit_groups_a_skipped_conversion_by_its_code(env, monkeypatch, tmp_path, capsys):
    """The plan line holds the code of the conversion's result, so the audit of a plan names why each file stays."""
    mp4_named_mkv(env)
    os.link(env["path"], env["path"] + ".download")
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 70, "movieId": 7, "path": env["path"],
                                                                              "mediaInfo": {"audioStreamCount": 2}})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    plans = tmp_path / "plans.jsonl"
    hook.main(["--backfill", "radarr", "--plan-out", str(plans)])
    assert decided(env)["outcome"] == json.loads(plans.read_text())["outcome"] == "repack_hardlinked"
    assert 'summary: {"repack_hardlinked": 1}' in capsys.readouterr().out
    hook.main(["--audit", "radarr", "--plan-from", str(plans)])
    assert "1 repack_hardlinked, not Matroska: MP4/QuickTime (Film A (1979))" in capsys.readouterr().out


def test_a_dry_run_limit_counts_a_conversion_that_would_run(env, monkeypatch):
    mp4_films(env, monkeypatch, 2)
    hook.main(["--backfill", "radarr", "--convert", "--limit", "1"])
    assert [r["outcome"] for r in log_lines(env) if r.get("outcome")] == ["would_repack"]


def test_an_alert_says_old_file_restored_only_when_the_job_s_own_old_file_came_back(monkeypatch):
    """The title of a re-grab says restored when the job's own old file came back. The decision line keeps regrabbed."""
    monkeypatch.setitem(hook.ARR, "radarr", types.SimpleNamespace(name="Radarr"))
    back = [{"file_id": 12, "result": "restored", "old": "/m/b.mkv"}, {"file_id": 11, "result": "not restored: x", "old": "/m/a.mkv"}]
    facts = hook.restore_facts("radarr", {"file_id": "11"}, back)
    assert (facts["came"], facts["others"], facts["stayed"]) == ([], 1, "x")   # another file of the download came back
    assert hook.action_code(dict(facts, code="regrabbed")) == "regrabbed"
    back[1]["result"] = "restored"
    facts = hook.restore_facts("radarr", {"file_id": "11"}, back)
    assert facts["came"] == ["a.mkv"] and facts["stayed"] is None
    assert [hook.action_code(dict(facts, code=c)) for c in ("regrabbed", "failed", "would_regrab")] == ["restored", "failed", "would_regrab"]
    assert hook.action_code(dict(hook.restore_facts("radarr", {"file_id": "11"}, []), code="regrabbed")) == "regrabbed"


@pytest.mark.parametrize("check", ["pass", "fail"])
def test_a_real_mp4_passes_the_conversion_proof(media, tmp_path, monkeypatch, check):
    """A real MP4 from ffmpeg under a .mkv name, converted by the real mkvmerge and proven by the real ffmpeg. A failed
    check removes the temp file and keeps the original. The name stays, so the app needs no re-link."""
    if not shutil.which("mkvmerge"):
        pytest.skip("needs mkvmerge")
    path = tmp_path / "Real (2020).mkv"
    shutil.copy(media / "good.mp4", path)
    os.chmod(path, 0o640)
    before = path.read_bytes()
    if check == "fail":
        monkeypatch.setattr(hook.arr_decide, "REPAIR_END", -1)   # every duration fails
    _, result, info, now = hook.convert("radarr", str(path), hook.mkvmerge(str(path)), os.stat(path), True, {})
    assert os.listdir(tmp_path) == [path.name] and now == str(path), os.listdir(tmp_path)
    if check == "fail":
        assert result.startswith("repack failed: the video and the audio end at") and path.read_bytes() == before and "new_size" not in info, result
        return
    assert result == "repacked" and open(path, "rb").read(4) == b"\x1a\x45\xdf\xa3" and os.stat(path).st_mode & 0o777 == 0o640, (result, info)
    assert [(p["stream"], p["method"], p["match"]) for p in info["proof"]] == [
        ("video 0", f"packets, original through {AUD}, new file through {AUD}", True), ("audio 1", "packets", True)]


# --- tool output and containers mkvmerge cannot read --------------------------------------------

def test_tool_output_with_a_latin1_byte_never_breaks_a_check(tmp_path, monkeypatch):
    """A tool may print byte 0xc4, and a strict decode fails on UnicodeDecodeError. Every tool call replaces it."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    scripts = {"mkvmerge": """printf '{"container": {"type": "Matroska"}, "tracks": [{"type": "audio", "properties": {"track_name": "Espa\\304ol"}}]}'""",
               "ffmpeg": """printf '[x] [info] title: Espa\\304ol\\n[x] [info] n_samples: 20\\n[x] [info] max_volume: -4.0 dB\\n' >&2""",
               "ffprobe": """printf 'Espa\\304ol\\n' >&2
case "$*" in *packet=pts_time*) printf '{"packets": [{"pts_time": "5.0"}], "format": {"tags": {"title": "Espa\\304ol"}}}' ;;
  *format=duration*) echo 600.0 ;; *) echo 0 ;; esac"""}
    for name, body in scripts.items():
        (bin_dir / name).write_text("#!/bin/sh\n" + body + "\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    assert hook.mkvmerge("x.mkv")["tracks"][0]["properties"]["track_name"] == "Espa\ufffdol"
    assert hook.sample("x.mkv", 0, 0)["n"] == 20 and hook.ffprobe_audio("x.mkv") == ["0"] and hook.ffprobe_duration("x.mkv") == 600.0
    assert hook.arr_meta.last_packet("x.mkv") == 5.0


WMV = {"container": {"recognized": True, "supported": False, "type": "Windows Media (ASF/WMV)"}, "tracks": [], "errors": []}


@pytest.mark.parametrize("ffprobe, samples, doubts", [
    ("0\n", "ok", []),
    ("0\n", "silent", ["the audio is silent at all 3 places checked, but this is unconfirmed, because the file cannot be read properly"]),
    ("", "ok", ["the file cannot be read properly, and no audio track was found"]),
])
def test_a_container_mkvmerge_cannot_read_is_checked_by_ffprobe_alone(env, ffprobe, samples, doubts):
    """In a .wmv file mkvmerge lists no tracks for ASF, so ffprobe finds the audio and ffmpeg samples it. It is
    never a disagreement, and with one tool a fault is never certain."""
    env["ffprobe_out"] = ffprobe
    if samples == "silent":
        env["ffmpeg_out"] = [SILENCE]
    certain, got, sampled = hook.check_audio(env["path"], WMV, [], runtime=10)
    assert certain is None and got == doubts and len(sampled) == (3 if ffprobe else 0), got
    assert all(argv[argv.index("-map") + 1] == "0:a:0" for argv in env["ffmpeg"])


def test_a_bad_plan_line_is_skipped_with_a_line(tmp_path, capsys):
    plans = tmp_path / "plans.jsonl"
    plans.write_text(json.dumps({"app": "radarr", "label": "Old", "path": "/m/old.mkv", "edits": []}) + "\n"
                     + json.dumps(plan_line("Movie 1", "undecided", undecided="x")) + "\n")
    assert [r["label"] for r in hook.plan_rows(str(plans), "radarr")] == ["Movie 1"]
    assert "skipped a plan line without class, undecided, dropped, cls, orig, tracks: Old" in capsys.readouterr().out




# --- corrupt video --------------------------------------------------------------------------------
# tests/fixtures/video_windows.json holds window shapes of real files. The seek noise of a UHD remux or of an
# MPEG-TS seek must read clean, and the real faults bad.

VIDEO_SHAPES = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "video_windows.json")))


def showinfo(n, first=0.0, fps=24.0):
    return [f"[Parsed_showinfo_0 @ 0x1] [info] n: {i} pts: {i} pts_time:{first + i / fps:.6g} duration:1" for i in range(n)]


@pytest.mark.parametrize("shape", VIDEO_SHAPES, ids=[s["name"][:50] for s in VIDEO_SHAPES])
def test_calibrated_window_shapes(shape):
    w = hook.arr_decide.parse_window("\n".join(shape["before"] + showinfo(shape["frames"], shape["first"], shape["fps"]) + shape["after"]))
    assert hook.arr_decide.bad_window(w) == shape["bad"], w


def ebml(segment_size, data):
    """A Matroska file: the EBML header, a Segment that promises segment_size bytes, then data."""
    return bytes.fromhex("1a45dfa3" "84" "42860181" "18538067") + (0x01 << 56 | segment_size).to_bytes(8, "big") + data


def test_segment_size_and_zero_probe_on_made_up_bytes(tmp_path, monkeypatch, settings):
    settings(log=str(tmp_path / "log.jsonl"))
    f = tmp_path / "v.mkv"
    f.write_bytes(noise(64 << 20))
    assert hook.zero_probe(str(f)) == ([], 256 * 65536, False) and hook.segment_short(str(f)) is None
    step = (64 << 20) * 0.98 / 256   # 257 KiB between two reads
    at = int((64 << 20) * (0.01 + 100.5 * step / (64 << 20))) // 4096 * 4096   # read 100
    for run in (4 << 10, 40 << 10):   # encoder padding: one page, and a long run of 40 KiB
        with open(f, "r+b") as g:
            g.seek(at); g.write(bytes(run))
        assert hook.zero_probe(str(f))[0] == [], run
    with open(f, "r+b") as g:   # 270 KiB from 20 KiB before read 100: a hit only when the run is measured both ways
        g.seek(at - (20 << 10)); g.write(bytes(270 << 10))
    assert len(hook.zero_probe(str(f))[0]) == 1
    with open(f, "rb") as g:
        assert hook.zero_run(g, at) == (256 << 10, 262 << 10)   # 250 KiB forward, then 6 KiB back reach the cap
    with open(f, "r+b") as g:
        g.seek(32 << 20); g.write(bytes(4 << 20))   # 4 MiB of 64 MiB: 16 of 256 offsets, give or take one
    for again in (False, True):
        hits = hook.zero_probe(str(f), again)[0]
        assert len(hits) in range(15, 20), hits
    certain, doubts, fields = hook.check_video(str(f), 0)   # certain before any window, so no decoder runs
    assert certain.startswith("the file has blank gaps at") and doubts == [] and fields["fault"] == "zero-filled" and "windows" not in fields
    assert fields["header"] == {"skipped": "not Matroska, or no Segment size"}
    assert fields["zeros"]["read"] - 256 * 65536 in range(1, 20 * (256 << 10))   # the runs were measured, up to 256 KiB a hit
    small = tmp_path / "small.mkv"   # under 18 MB the reads overlap. Padding still never counts, and a long run counts per 64 KiB.
    small.write_bytes(noise(512 << 10) + bytes(40 << 10) + noise(512 << 10))
    assert hook.zero_probe(str(small))[0] == []
    small.write_bytes(noise(512 << 10) + bytes(300 << 10) + noise(512 << 10))
    assert len(hook.zero_probe(str(small))[0]) in range(2, 7)
    f.write_bytes(ebml(1000, bytes(900)))
    assert hook.segment_short(str(f)) == 100   # the Segment promises 1000 bytes of data, 900 are there
    f.write_bytes(ebml(200000, noise(100000)))
    certain, doubts, fields = hook.check_video(str(f), 0)
    assert (certain, fields["fault"], fields["header"]["short"]) == ("the file is 0.1 MB smaller than it should be, so the download is incomplete", "truncated", 100000)
    assert "zeros" not in fields
    # the hook's own edit of this file failed or was killed: a short header is then only a doubt that says so
    hook.log(dict(path=str(f), result="edited")); hook.log(dict(path=str(f), result="editing"))
    certain, doubts, fields = hook.check_video(str(f), 0)
    assert certain is None and doubts == ["the file may be missing 100000 bytes at its end, after its last flag edit failed"]
    hook.log(dict(path=str(f), result="editing", undo=[]))
    hook.log(dict(path=str(f), result="wrong content: x", edit_result="edited"))   # the edit finished
    assert hook.check_video(str(f), 0)[2]["fault"] == "truncated"
    f.write_bytes(ebml(1000, noise(1000 - 67)))   # 67 bytes short, every cluster intact
    certain, doubts, fields = hook.check_video(str(f), 0)
    assert certain is None and doubts == ["the file may be missing 67 bytes at its end"]
    assert fields["windows"] == {"list": [], "took": fields["windows"]["took"], "read": 0, "skipped": "the duration is under 60 s"}
    f.write_bytes(bytes.fromhex("1a45dfa3"))   # a header cut inside its first element is not a Segment size
    assert hook.segment_short(str(f)) is None


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """An ffmpeg on PATH that reads the whole input file, then sleeps, so a window runs until something stops it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ffmpeg").write_text('#!/bin/sh\nwhile [ "$1" != "-i" ]; do shift; done\ncat "$2" > /dev/null\nexec sleep 30\n')
    (bin_dir / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hook, "WINDOW_POLL", 0.05)
    f = tmp_path / "v.mkv"
    f.write_bytes(noise(4 << 20))
    return str(f)


@pytest.mark.parametrize("limit, stopped", [("VIDEO_MAX_READ", "read over 1 MiB"), ("VIDEO_TIMEOUT", "ran over 1 s")])
def test_a_window_stops_at_the_read_cap_or_the_time_cap_and_only_doubts(fake_ffmpeg, monkeypatch, limit, stopped):
    monkeypatch.setattr(hook.arr_decide, limit, 1 << 20 if limit == "VIDEO_MAX_READ" else 1)
    started = time.monotonic()
    w = hook.window(fake_ffmpeg, 300, 5)
    assert w["stopped"] == stopped and not w["ran"] and time.monotonic() - started < 10
    assert w["read"] >= 4 << 20   # the bytes the child read reach our own count once it is reaped
    certain, doubts = hook.arr_decide.video_verdict([], [w, w, w])
    # The read cap with no error is only logged: a file that indexes its video once decodes clean. The time cap doubts.
    want = [] if limit == "VIDEO_MAX_READ" else [f"the video check at 5:00 {stopped}, so the file may have no usable index"] * 3
    assert certain is None and doubts == want


def test_the_job_time_limit_kills_the_decoder(fake_ffmpeg, monkeypatch):
    procs, real = [], hook.subprocess.Popen
    monkeypatch.setattr(hook.subprocess, "Popen", lambda *a, **k: procs.append(real(*a, **k)) or procs[-1])

    def rchar(pid="self"):   # the job's alarm fires during the first check of the window
        if pid != "self":
            raise hook.arr_meta.OutOfTime("stopped after 300 seconds")
        return 0
    monkeypatch.setattr(hook, "rchar", rchar)
    with pytest.raises(hook.arr_meta.OutOfTime):
        hook.window(fake_ffmpeg, 0, 5)
    assert procs[0].poll() is not None


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """Made-up clips for real ffmpeg: 2 minutes of 640x360 h264, then copies damaged the ways real files are."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    d = tmp_path_factory.mktemp("clips")
    good = d / "good.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=120", "-c:v", "libx264",
                    "-preset", "ultrafast", "-g", "48", "-b:v", "2M", str(good)], check=True)
    for name, container in (("good.ts", "mpegts"), ("cut.mp4", "mp4")):
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(good), "-c", "copy", "-movflags", "+faststart", "-f", container, str(d / name)],
                       check=True)
    os.truncate(d / "cut.mp4", os.path.getsize(d / "cut.mp4") * 8 // 10)
    size = os.path.getsize(good)
    shutil.copy(good, d / "truncated.mkv")
    os.truncate(d / "truncated.mkv", size * 8 // 10)   # a cut download
    shutil.copy(good, d / "zeroed.mkv")
    with open(d / "zeroed.mkv", "r+b") as f:
        f.seek(size * 4 // 10); f.write(bytes(size // 10))   # an unfinished region of the download
    shutil.copy(good, d / "damaged.mkv")
    with open(d / "damaged.mkv", "r+b") as f:
        for k in range(1, 60):   # 59 spots of 16 KiB random bytes, so each window meets one
            f.seek(size * k // 60); f.write(noise(16 << 10))
    return d


@pytest.mark.parametrize("name, fault", [("good.mkv", None), ("good.ts", None), ("truncated.mkv", "truncated"), ("zeroed.mkv", "zero-filled"),
                                         ("damaged.mkv", "bad windows"), ("cut.mp4", None)])
def test_video_check_on_generated_clips(clips, name, fault):
    certain, doubts, fields = hook.check_video(str(clips / name), 120.0)
    assert fields["fault"] == fault and bool(certain) == bool(fault), (certain, doubts)
    if name.startswith("good"):
        assert doubts == [] and [w["frames"] for w in fields["windows"]["list"]] == [120, 120, 120]
    if fault == "bad windows":   # decode errors after the first frame, from the damage each window meets
        assert certain.startswith("the video is broken at 3 of 3 places checked") and all(w["errors"] for w in fields["windows"]["list"])
    if name == "cut.mp4":   # not Matroska, so no header stage, and the empty late window is only a doubt
        assert fields["header"] == {"skipped": "not Matroska, or no Segment size"} and doubts == ["no video at 1:42"], doubts
    if not fault or fault == "bad windows":
        stage = fields["windows"]
        assert stage["read"] == sum(w["read"] for w in stage["list"]) > 0 and all(w["took"] > 0 for w in stage["list"])
    assert fields["zeros"]["read"] >= 256 * 65536 if fault != "truncated" else "zeros" not in fields   # more when zero runs were measured


# Rules A to F came from full decodes of flagged files that the checks had judged wrong.

def test_the_zero_probe_reads_only_the_matroska_clusters(tmp_path):
    """Rule A. A file may hold a zero run in a font of its Attachments, before the first Cluster. Another may hold
    zeros from its Segment end on, then the Clusters of an older copy. The episode in each Segment decodes clean, so
    neither zero run is damage."""
    D, rnd = hook.arr_decide, random.Random(20260927)   # the same bytes on every run
    head = el(0x1549A966, rnd.randbytes(200)) + el(0x1941A469, bytes(4 << 20))   # Info, then Attachments with a zero-filled font
    segment = head + b"".join(el(D.CLUSTER, rnd.randbytes(1 << 20)) for _ in range(24)) + el(D.CUES, rnd.randbytes(4096))
    old = b"".join(el(D.CLUSTER, rnd.randbytes(1 << 20)) for _ in range(8))
    f = tmp_path / "Show K - s01e04 - Bluray-1080p.mkv"
    f.write_bytes(ebml(len(segment), segment) + bytes(16 << 20) + old)
    ds, size = len(ebml(0, b"")), os.path.getsize(f)
    with open(f, "rb") as g:
        assert hook.zero_span(g, size) == (ds + len(head), ds + len(segment))
    for again in (False, True):
        assert hook.zero_probe(str(f), again)[0] == []
    assert hook.check_video(str(f), 0)[:2] == (None, [])
    with open(f, "r+b") as g:   # an unfinished download: 2 MiB of zeros at two places inside the Clusters
        for at in (ds + len(head) + (6 << 20), ds + len(head) + (15 << 20)):
            g.seek(at); g.write(bytes(2 << 20))
    hits = hook.zero_probe(str(f))[0]
    assert len(hits) >= 2 and all((ds + len(head)) / size < h < (ds + len(segment)) / size for h in hits), hits   # shares of the file
    assert hook.check_video(str(f), 0)[2]["fault"] == "zero-filled"
    with open(f, "r+b") as g:   # a zero run is measured only inside the span, never into the zeros past the Segment end
        g.seek(ds + len(segment) - 65536); g.write(bytes(65536))
    with open(f, "rb") as g:
        assert hook.zero_run(g, ds + len(segment) - 65536, ds + len(head), ds + len(segment)) == (65536, 65536 + 65536)
        assert hook.zero_run(g, ds + len(segment) - 65536)[0] == hook.arr_decide.ZERO_RUN   # unbounded, it reaches the cap


# A WEBDL-1080p episode, the window at 263 s. The video track is encrypted.
NO_DECODER_WINDOW = [
    "[matroska,webm @ 0x5a5a00001000] [error] mov FourCC not found encv.",
    "[matroska,webm @ 0x5a5a00001000] [info] Unknown/unsupported AVCodecID V_QUICKTIME.",
    "[matroska,webm @ 0x5a5a00001000] [warning] Could not find codec parameters for stream 0 (Video: none (encv / 0x76636E65), none, "
    "1920x1080): unknown codec",
    "[vist#0:0/none @ 0x5a5a00002000] [error] Decoding requested, but no decoder found for: none",
    "[error] Error opening output file -.",
    "[fatal] Error opening output files: Invalid argument"]


def test_an_encrypted_video_track_with_no_decoder_is_certain():
    """Rule D. ffmpeg has no decoder for encrypted video and exits 234 with no frame, so the windows read as could not
    run and the file stayed a doubt. A full decode output 0 frames: the episode has no picture. The encv FourCC in the
    demuxer's lines is the evidence of encryption."""
    w = hook.arr_decide.parse_window("\n".join(NO_DECODER_WINDOW), 234)
    assert w["nodecoder"] and w["encrypted"] and not w["ran"] and w["empty"]
    wins = [dict(w, at=at) for at in (263, 1316, 2237)]
    assert hook.arr_decide.video_verdict([], wins) == (
        "the video is encrypted and cannot play at 3 of 3 places checked", [])
    ok = dict(CLEAN_WINDOW, at=1316)   # one such window stays a doubt
    assert hook.arr_decide.video_verdict([], [wins[0], ok, ok]) == (None, ["the video at 4:23 could not be checked"])


def test_a_codec_ffmpeg_does_not_know_is_only_a_doubt(tmp_path):
    """A healthy file in a codec ffmpeg does not know logs the same no-decoder line. A re-grab would delete
    it, so with no evidence of encryption its windows stay windows that did not run."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    f = tmp_path / "unknown.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=90", "-c:v", "libx264",
                    "-preset", "ultrafast", str(f)], check=True)
    b = bytearray(f.read_bytes()); i = b.find(b"V_MPEG4/ISO/AVC"); b[i:i + 15] = b"V_MPEG4/ISO/AVX"; f.write_bytes(b)
    certain, doubts, fields = hook.check_video(str(f), 90.0)
    wins = fields["windows"]["list"]
    assert certain is None and all(w["nodecoder"] and not w["encrypted"] for w in wins), wins
    assert doubts == [f"the video at 0:{at:02d} could not be checked" for at in (9, 45)] + ["the video at 1:16 could not be checked"]


def test_the_bytes_show_an_encrypted_track_when_ffmpeg_names_none(tmp_path, monkeypatch):
    """The evidence of encryption from the file itself: a ContentEncryption element in a Matroska video
    TrackEntry, or an encv sample entry with a sinf box that holds a tenc box in an MP4 moov (ffmpeg writes one)."""
    D = hook.arr_decide
    enc = el(D.CONTENTENCODINGS, el(D.CONTENTENCODING, el(0x5033, b"\x01") + el(D.CONTENTENCRYPTION, el(0x47E1, b"\x05") + el(0x47E2, bytes(16)))))
    entry = lambda kind, extra: el(D.TRACKENTRY, el(0xD7, b"\x01") + el(D.TRACKTYPE, bytes([kind])) + el(0x86, b"V_MPEG4/ISO/AVC") + extra)
    for name, tracks, want in (("enc.mkv", entry(1, enc), True), ("plain.mkv", entry(1, b""), False), ("audio.mkv", entry(1, b"") + entry(2, enc), False)):
        segment = el(0x1549A966, noise(40)) + el(D.TRACKS, tracks) + el(D.CLUSTER, noise(4096))
        (tmp_path / name).write_bytes(ebml(len(segment), segment))
        assert hook.encrypted_video(str(tmp_path / name)) is want, name
    nodecoder = dict(CLEAN_WINDOW, frames=0, empty=True, ran=False, nodecoder=True, encrypted=False, read=0, took=1.0)
    monkeypatch.setattr(hook, "window", lambda path, start, secs: dict(nodecoder, at=round(start)))
    monkeypatch.setattr(hook, "zero_probe", lambda *a, **k: ([], 0, False))
    assert hook.check_video(str(tmp_path / "enc.mkv"), 600.0)[0].startswith("the video is encrypted and cannot play")
    assert hook.check_video(str(tmp_path / "plain.mkv"), 600.0)[0] is None
    if not shutil.which("ffmpeg"):
        return
    for name, args in (("cenc.mp4", ["-encryption_scheme", "cenc-aes-ctr", "-encryption_key", "76a6c65c5ea762046bd749a2e632ccbb",
                                     "-encryption_kid", "a7e61c373e219033c21091fa607bf3b8"]), ("clear.mp4", [])):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3", "-c:v", "libx264",
                        "-preset", "ultrafast", *args, str(tmp_path / name)], check=True)
    assert hook.encrypted_video(str(tmp_path / "cenc.mp4")) and not hook.encrypted_video(str(tmp_path / "clear.mp4"))


def test_windows_follow_the_video_stream_of_a_file_that_is_not_matroska(tmp_path):
    """Rule E. An episode (DVD, mp4) holds 23:36 of video, and one stray audio packet at 44:10 sets the format
    duration to 44:16. The window at 85 percent of the format duration lay past the video and found no frame. Its audio
    breaks at 11:48, and check_audio() never runs on an mp4, so the gap is a doubt."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    f = tmp_path / "Show L - s01e05 - DVD.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=70", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=150", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(f)], check=True)
    dur, hp = hook.video_inputs(str(f))
    assert hp is None and abs(dur - 70) < 0.1 and hook.ffprobe_duration(str(f)) > 149, dur
    certain, doubts, fields = hook.check_video(str(f), dur)
    assert certain is None and [w["at"] for w in fields["windows"]["list"]] == [7, 35, 60]
    assert doubts == ["the file's tracks run to 2:30, but the video stops at 1:10, so another track may be broken"], doubts


def video_regrabs(env):
    return hook.store.items("regrabs")


def test_corrupt_video_on_import_deletes_remonitors_and_fails_the_grab(env, monkeypatch):
    env["window_out"] = [BAD_WINDOW]
    grabbed(env, monkeypatch)
    hook.main([])
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "history/failed/2101", None)]
    assert env["mkvpropedit"] == [] and analyzes(env) == []   # no flag edit on a file about to go
    assert len(env["windows"]) == 6 and len(env["ffmpeg"]) == 3   # three windows, three more from scratch; the audio sampled once
    (rec,) = log_lines(env)
    f, act = action(env)
    assert (rec["outcome"], f["kind"]) == ("corrupt_video", "video")
    assert f["certain"] == "the video is broken at 3 of 3 places checked: 4 playback errors at 1:00, 4 playback errors at 5:00 and 4 playback errors at 8:30"
    assert act == {"code": "regrabbed", "name": "Radarr", "kind": "video", "n": 1, "came": [], "linked": True, "own_copy": True, "others": 0,
                   "stayed": None}
    assert [at for p, at in env["windows"]] == [60, 300, 510, 180, 420, 570]   # the second check decodes other parts
    v = rec["video"]
    assert v["fault"] == "bad windows" and v["header"] == {"skipped": "not Matroska, or no Segment size"} and v["zeros"]["hits"] == []
    assert v["zeros"]["read"] == 256 * 1000 and [w["at"] for w in v["windows"]["list"]] == [60, 300, 510] and v["windows"]["read"] == 3000
    assert video_regrabs(env) == {"radarr": [env["clock"][0]]}   # the one count of the app, as for broken audio


@pytest.mark.parametrize("broken", ["audio", "video"])
@pytest.mark.parametrize("left, capped", [(1, False), (0, True)])
def test_every_kind_of_regrab_shares_one_cap(env, monkeypatch, broken, left, capped):
    if broken == "video":
        env["window_out"] = [BAD_WINDOW]
    else:
        env["ffmpeg_out"] = [SILENCE]
    grabbed(env, monkeypatch)
    hook.store.put("regrabs", "radarr", [env["clock"][0] - 60] * (hook.CFG.regrab_cap - left))
    hook.main([])
    assert (env["writes"] == []) == capped
    if capped:
        assert log_lines(env)[0]["findings"][0]["action"] == {"code": "capped", "cap": hook.CFG.regrab_cap}
    counts = video_regrabs(env)
    assert list(counts) == ["radarr"] and len(counts["radarr"]) == hook.CFG.regrab_cap   # one count per app, no count per kind
    assert hook.CFG.regrab_cap == 30 and not hasattr(hook, "VIDEO_REGRAB_CAP")


def test_mixed_kinds_of_regrab_reach_the_cap_together(env, monkeypatch, settings):
    """A broken audio re-grab and a corrupt video re-grab fill a cap of 2. A third fault of either kind only alerts."""
    settings(regrab_cap=2)
    grabbed(env, monkeypatch)
    loud = env["ffmpeg_out"]
    for n, broken in enumerate(["audio", "video", "audio"]):
        env["ffmpeg_out"], env["window_out"] = ([SILENCE], [CLEAN_WINDOW]) if broken == "audio" else (loud, [BAD_WINDOW])
        monkeypatch.setenv("radarr_download_id", f"dl{n}")
        env["movies"][f"history?downloadId=dl{n}&pageSize=1000"] = GRAB
        writes = len(env["writes"])
        hook.main([])
        assert (len(env["writes"]) > writes) == (n < 2), (n, broken)
    assert len(video_regrabs(env)["radarr"]) == 2
    assert log_lines(env)[-1]["findings"][0]["action"] == {"code": "capped", "cap": 2}


def test_a_video_second_check_that_disagrees_keeps_the_file(env, monkeypatch):
    env["window_out"] = [BAD_WINDOW] * 3 + [CLEAN_WINDOW] * 3   # certain first, clean from scratch
    grabbed(env, monkeypatch)
    hook.main([])
    assert env["writes"] == [] and len(env["windows"]) == 6
    (rec,) = log_lines(env)
    f, act = action(env)
    assert (rec["outcome"], f["kind"], act) == ("corrupt_video", "video", {"code": "unconfirmed"})
    assert not hook.store.items("regrabs")


@pytest.mark.parametrize("where, error", [("segment_short", OSError("[Errno 5] Input/output error")),
                                          ("zero_probe", hook.arr_meta.OutOfTime("stopped after 300 seconds"))])
def test_a_failed_video_check_gives_no_verdict_and_never_costs_the_edit(env, monkeypatch, where, error):
    def boom(*a, **k):
        raise error
    monkeypatch.setattr(hook, where, boom)
    hook.main([])
    (rec,) = [r for r in log_lines(env) if r.get("outcome")]
    assert rec["outcome"] == "edited" and len(env["mkvpropedit"]) == 1 and "video_check_error" in rec["reasons"]
    assert rec["video"] == {"certain": None, "doubts": [], "fault": None, "error": f"{type(error).__name__}: {error}", "code": "video_check_error"}


def test_the_time_limit_outside_the_video_check_still_stops_the_job(env, monkeypatch):
    def late(*a, **k):
        raise hook.arr_meta.OutOfTime("stopped after 300 seconds")
    monkeypatch.setattr(hook, "check_audio", late)
    hook.main([])
    (rec,) = log_lines(env)
    assert rec["result"].startswith("error: OutOfTime") and env["mkvpropedit"] == [] and env["windows"] == []


def test_the_zero_probe_stops_at_the_reserve(env, monkeypatch):
    real, reads = hook.zero_probe, []
    def zero_probe(path, again=False, stop=lambda: False):   # enough time for the stage to start, then the job runs short mid-probe
        def late():
            if reads:
                env["clock"][0] = hook.DEADLINE.end - 20.0
            reads.append(path)
            return stop()
        return real(path, again, late)
    monkeypatch.setattr(hook, "zero_probe", zero_probe)
    hook.main([])
    (rec,) = [r for r in log_lines(env) if r.get("outcome")]
    zeros = rec["video"]["zeros"]
    assert zeros["skipped"] == "stopped with 20 s left of the job's time limit" and zeros["read"] < 256 * 1000
    assert rec["outcome"] == "edited" and env["windows"] == [] and "video" not in rec["alert_kinds"]


def test_one_bad_window_alerts_and_still_edits(env, monkeypatch):
    env["window_out"] = [BAD_WINDOW, CLEAN_WINDOW, CLEAN_WINDOW]
    grabbed(env, monkeypatch)
    hook.main([])
    assert env["writes"] == [] and len(env["mkvpropedit"]) == 1 and len(env["windows"]) == 3   # a doubt has no second check
    (rec,) = [r for r in log_lines(env) if r.get("outcome") == "edited"]
    assert rec["findings"] == [{"kind": "video", "doubts": ["4 playback errors at 1:00"]}] and rec["video"]["certain"] is None
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1


@pytest.mark.parametrize("left, zeros, windows", [(95.0, True, 3), (60.0, True, 0), (35.0, False, 0)])
def test_a_short_time_limit_skips_video_stages_and_never_costs_the_edit(env, monkeypatch, left, zeros, windows):
    """A stage starts only when the job's time limit holds its worst case plus VIDEO_RESERVE: 10 s for the zero probe,
    60 s for a window. A skipped stage is logged, and it is never a doubt or an alert."""
    env["window_out"] = [BAD_WINDOW]   # would be certain if any window ran
    monkeypatch.setattr(hook.DEADLINE, "left", lambda: left)
    hook.main([])
    (rec,) = [r for r in log_lines(env) if r.get("outcome")]
    assert len(env["windows"]) == windows and ("zeros" in rec["video"] and "hits" in rec["video"]["zeros"]) == zeros
    if windows:
        assert rec["outcome"] == "corrupt_video"
    else:
        assert rec["outcome"] == "edited" and "video" not in rec["alert_kinds"]
        stage = rec["video"]["windows" if zeros else "zeros"]
        assert stage["skipped"] == f"{left:.0f} s left of the job's time limit"


def test_later_jobs_of_a_video_unit_skip_only_the_video_check(env, monkeypatch, tmp_path):
    paths = pack(env, monkeypatch, tmp_path, broken=())
    hook.store.put("unit", "sonarr|pack1", {"time": env["clock"][0], "failed": True, "deleted": [201], "clean": [202], "kind": "video"})
    import_event(monkeypatch, paths, 201)
    import_event(monkeypatch, paths, 202)   # its video was checked with its download, its audio was not
    assert env["windows"] == [] and len(env["ffmpeg"]) == 3
    lines = [r for r in log_lines(env) if r.get("outcome")]
    assert (lines[0]["outcome"], lines[0]["fault"]) == ("deleted_with_download", "video")
    assert lines[1]["outcome"] == "edited" and "video already checked with its download" in lines[1]["notes"] and "video" not in lines[1]


def test_video_scan_resumes_is_read_only_and_posts_a_summary_only_for_a_run_with_a_problem(env, monkeypatch, tmp_path):
    scan_library(monkeypatch, tmp_path, 3)
    env["window_out"] = [BAD_WINDOW] * 3 + [CLEAN_WINDOW] * 6   # file 1 corrupt, files 2 and 3 fine
    hook.main(["--backfill", "radarr", "--check-video", "--limit", "2"])
    base = os.path.join(hook.CFG.state_dir, "video-scan-radarr")
    assert scan_state(base)["last"] == 102
    hook.main(["--backfill", "radarr", "--check-video"])
    assert scan_state(base)["checked"] == 3 and len(env["windows"]) == 9 and env["ffmpeg"] == []
    posts = [b["embeds"][0] for m, u, b in env["http"] if m == "POST"]
    assert [p["title"] for p in posts] == [f"Video scan: Radarr {hook.CFG.instance}"] and posts[0]["color"] == hook.COLORS["amber"]
    assert posts[0]["description"] == "2 files checked this run, 2 of 3 in this pass."
    assert open(base + ".txt").read().startswith("BROKEN\tMovie 1 (2000)\tthe video is broken at 3 of 3 places checked")
    (row,) = scan_rows(base)
    assert row["video"]["fault"] == "bad windows" and len(row["video"]["windows"]["list"]) == 3
    assert env["writes"] == [] and env["mkvpropedit"] == []   # read-only: no edit, no re-grab, one decision line per file
    lines = log_lines(env)
    assert [(r["source"], r["outcome"]) for r in lines] == [("video_scan", "corrupt_video")] + [("video_scan", "video_checked")] * 2
    assert lines[0]["video"]["certain"].startswith("the video is broken at 3 of 3") and scan_state("audio-scan-radarr") is None
    assert env["sleeps"].count(hook.SCAN_PACE) == 3 and env["events"].count("lock") == 3
    with pytest.raises(SystemExit):
        hook.main(["--backfill", "radarr", "--check-audio", "--check-video"])


def scan_library(monkeypatch, tmp_path, n):
    """n Radarr films m1.mkv to mn.mkv, file ids 101 on, for a library scan. Returns their paths."""
    movies = []
    for i in range(1, n + 1):
        f = tmp_path / "media" / f"m{i}.mkv"; f.write_bytes(b"x")
        movies.append({"id": i, "title": f"Movie {i}", "year": 2000, "originalLanguage": {"name": "English"},
                       "movieFile": {"id": 100 + i, "path": str(f)}})
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    return [m["movieFile"]["path"] for m in movies]


def test_scan_state_moves_only_over_checked_files():
    state, order = {"last": -1, "checked": 0, "done": []}, collections.deque([101, 102, 103, 105])
    hook.advance(state, order, 103); hook.advance(state, order, 105)   # out of order, 101 and 102 still in flight
    assert (state["last"], state["done"], state["checked"]) == (-1, [103, 105], 2)
    hook.advance(state, order, 101)
    assert (state["last"], state["done"]) == (101, [103, 105])
    hook.advance(state, order, 102)
    assert (state["last"], state["done"], state["checked"], list(order)) == (105, [], 4, [])


def test_a_parallel_scan_stops_cleanly_and_a_restart_never_skips(env, monkeypatch, tmp_path):
    scan_library(monkeypatch, tmp_path, 4)
    base = os.path.join(hook.CFG.state_dir, "video-scan-radarr")
    killed, stopping, seen = threading.Event(), [True], []
    monkeypatch.setattr(hook, "kill_children", killed.set)

    def check(path, dur, again=False, hp=None):
        name = os.path.basename(path); seen.append(name)
        if name == "m1.mkv" and stopping[0]:   # slow: the other three finish first, then a SIGTERM arrives
            for _ in range(200):
                if (scan_state(base) or {}).get("done") == [102, 103, 104]: break
                threading.Event().wait(0.05)
            os.kill(os.getpid(), signal.SIGTERM)
            assert killed.wait(10)   # the stop kills the processes of the file in flight, and its result is dropped
            return "cut short by the stop", [], {"fault": "bad windows"}
        return (None, [f"doubt in {name}"], {"fault": None}) if name in ("m2.mkv", "m4.mkv") else (None, [], {"fault": None})
    monkeypatch.setattr(hook, "check_video", check)
    hook.main(["--backfill", "radarr", "--check-video", "--workers", "4"])
    assert scan_state(base)["done"] == [102, 103, 104] and scan_state(base)["last"] == -1
    assert sorted(r["ids"]["file_id"] for r in log_lines(env)) == [102, 103, 104]   # no line for the dropped file
    assert "BROKEN" not in open(base + ".txt").read()
    stopping[0] = False
    hook.main(["--backfill", "radarr", "--check-video", "--workers", "2"])   # the restart checks only the dropped file
    state = scan_state(base)
    assert (state["last"], state["done"], state["checked"]) == (104, [], 4) and sorted(seen) == ["m1.mkv"] * 2 + ["m2.mkv", "m3.mkv", "m4.mkv"]
    assert [line.split("\t")[1] for line in open(base + ".txt")] == ["Movie 2 (2000)", "Movie 4 (2000)"]   # in file id order
    posts = [b["embeds"][0]["description"] for m, u, b in env["http"] if m == "POST"]
    assert posts == ["3 files checked this run, 3 of 4 in this pass. The run was stopped."]   # the restart found nothing, so it posts nothing
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL   # the scan puts the handlers back


def test_scan_locks_are_shared_and_a_waiting_edit_blocks_new_ones(env):
    a, b = hook.locked(shared=True), hook.locked(shared=True)   # two scan files read side by side
    order = []
    edit = threading.Thread(target=lambda: (f := hook.locked(), order.append("edit"), f.close()), daemon=True)
    edit.start()
    with open(os.path.join(hook.CFG.state_dir, "lock.gate"), "w") as gate:   # wait until the edit holds the gate
        for _ in range(200):
            try:
                REAL_FLOCK(gate, fcntl.LOCK_EX | fcntl.LOCK_NB); REAL_FLOCK(gate, fcntl.LOCK_UN)
            except BlockingIOError:
                break
            threading.Event().wait(0.01)
    new = threading.Thread(target=lambda: hook.locked(shared=True).close() or order.append("scan"), daemon=True)
    new.start()
    threading.Event().wait(0.3)
    assert order == []   # the edit waits for the two files in flight, and a new scan file waits behind the edit
    a.close(); b.close()
    edit.join(5); new.join(5)
    assert order == ["edit", "scan"]


def test_kill_children_ends_a_child_process():
    p = subprocess.Popen(["sleep", "30"])
    hook.kill_children()
    assert p.wait(timeout=5) == -signal.SIGKILL


def test_a_scan_takes_its_workers_from_the_env_file_unless_given(env, monkeypatch, settings, tmp_path, capsys):
    scan_library(monkeypatch, tmp_path, 2)
    settings(scan_workers=3)
    hook.main(["--backfill", "radarr", "--check-video", "--limit", "1"])
    hook.main(["--backfill", "radarr", "--check-video", "--workers", "1"])   # an explicit --workers wins
    out = capsys.readouterr().out
    assert "files left in this pass, 3 workers" in out and "files left in this pass, 1 workers" in out


@pytest.mark.parametrize("args", [["--check-video", "--workers", "0"], ["--apply", "--workers", "2"]])
def test_workers_needs_one_or_more_and_a_dry_run_or_a_scan(args):
    with pytest.raises(SystemExit):
        hook.backfill(["radarr"] + args)


# --- a file the app moved before the worker reached it --------------------------------------------

def moved_film(env, monkeypatch, answer):
    """The job names the import path. The app renamed the file since. answer(new) is the app's moviefile/11, or an
    exception to raise. Returns the new path."""
    new = os.path.join(os.path.dirname(env["path"]), "Film A (1979) Bluray-1080p.mkv")
    os.rename(env["path"], new)
    monkeypatch.setenv("radarr_moviefile_id", "11")
    env["plex_items"] = [plex_item("7101", "tmdb://90001", new)]
    env["movies"]["rootfolder"] = [{"path": os.path.dirname(os.path.dirname(env["path"])) + "/"}]
    got = answer(new)
    def arr(app, p):
        if p == "moviefile/11" and isinstance(got, Exception): raise got
        return got if p == "moviefile/11" else env["movies"][p]
    monkeypatch.setattr(hook, "arr", arr)
    return new


def test_a_moved_file_is_checked_edited_and_analyzed_at_its_new_path(env, monkeypatch):
    new = moved_film(env, monkeypatch, lambda new: {"id": 11, "movieId": 7, "path": new})
    hook.main([])
    moved, editing, rec, plex = log_lines(env)
    assert (moved["result"], moved["old_path"], moved["path"]) == ("file_moved", env["path"], new)
    assert editing["undo"][1] == new and rec["outcome"] == "edited" and rec["path"] == new
    assert rec["moved_from"] == env["path"] and rec["reasons"][0] == "file_moved"
    assert plex["path"] == new and analyzes(env) == ["/library/metadata/7101/analyze"]


def http_error(code):
    return urllib.error.HTTPError("http://127.0.0.1:7878/api/v3/moviefile/11", code, "x", {}, io.BytesIO(b""))


@pytest.mark.parametrize("answer, note", [
    (lambda new: http_error(404), "Radarr no longer has file 11"),   # an upgrade or a delete
    (lambda new: {"id": 11, "movieId": 8, "path": new}, "Radarr file 11 belongs to movie 8 now"),
    (lambda new: {"id": 11, "movieId": 7, "path": new + ".gone"}, "which is missing too"),
    (lambda new: {"id": 11, "movieId": 7, "path": shutil.copy(new, os.path.dirname(os.path.dirname(os.path.dirname(new))))},
     "outside its root folders"),
    (lambda new: http_error(500), None)])
def test_a_moved_file_is_dropped_only_when_the_app_no_longer_has_it_for_this_item(env, monkeypatch, answer, note):
    moved_film(env, monkeypatch, answer)
    hook.main([])
    (rec,) = log_lines(env)
    assert note in rec["note"] if note else rec["result"] == "error: HTTPError: HTTP Error 500: x"
    assert rec["outcome"] == ("file_gone" if note else "error") and env["mkvpropedit"] == [] and queue(env) == []


@pytest.mark.parametrize("eps, outcome", [([31, 32], "edited"), ([31], "file_gone")])
def test_a_moved_episode_file_must_hold_the_same_episodes(env, monkeypatch, eps, outcome):
    episodes = [{"id": i, "seasonNumber": 1, "episodeNumber": i - 30, "runtime": 44} for i in eps]
    as_sonarr(monkeypatch, env, {"title": "Show", "tvdbId": 1, "originalLanguage": {"name": "English"}}, episodes)
    new = os.path.join(os.path.dirname(env["path"]), "Show - S01E01E02.mkv")
    os.rename(env["path"], new)
    real = hook.arr
    answers = {"episodefile/9": {"id": 9, "seriesId": 5, "path": new}, "rootfolder": [{"path": os.path.dirname(os.path.dirname(new))}]}
    monkeypatch.setattr(hook, "arr", lambda app, p: answers[p] if p in answers else real(app, p))
    monkeypatch.setenv("sonarr_episodefile_episodeids", "31,32")
    hook.main([])
    rec = [r for r in log_lines(env) if "outcome" in r][0]
    assert rec["outcome"] == outcome and rec["path"] == (new if outcome == "edited" else env["path"])
    assert outcome == "edited" or rec["note"] == "Sonarr file 9 holds episodes [31] now"


def test_a_moved_broken_file_is_regrabbed_by_its_new_path(env, monkeypatch):
    new = moved_film(env, monkeypatch, lambda new: {"id": 11, "movieId": 7, "path": new})
    env["ffmpeg_out"] = [SILENCE]
    grabbed(env, monkeypatch)
    env["movies"]["history?downloadId=a1b2c3d4&pageSize=1000"] = GRAB
    hook.main([])
    assert env["writes"][0] == ("DELETE", "moviefile/11", None)
    assert {a[a.index("-i") + 1] for a in env["ffmpeg"]} == {new} and len(env["ffmpeg"]) == 6   # the check and the second check


# --- parallel job processes -----------------------------------------------------------------------

def trace(what, **kw):
    """One line in the shared trace file, from any process. perf_counter is one clock for every process."""
    fd = os.open(os.path.join(hook.CFG.state_dir, "trace"), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    os.write(fd, (json.dumps(dict(pid=os.getpid(), what=what, t=time.perf_counter(), **kw)) + "\n").encode())
    os.close(fd)


def traced(what=None):
    try:
        rows = [json.loads(line) for line in open(os.path.join(hook.CFG.state_dir, "trace"))]
    except FileNotFoundError:
        return []
    return [r for r in rows if what is None or r["what"] == what]


def real_fork():
    """A real fork with no connection to the store across it, see store.fork(). The env fixture fakes os.fork."""
    hook.store.close()
    return REAL_FORK()


def real_wait(seconds):
    threading.Event().wait(seconds)   # time.sleep is the fixture's fake clock


@pytest.fixture
def pool(env, monkeypatch, settings):
    """The env host with real job processes. It has a real fork and os._exit, HOOK_WORKERS 3, and a trace of every Plex
    PUT and every mkvpropedit with the process that ran it. The coordinator runs in the test process."""
    monkeypatch.setattr(hook.os, "fork", REAL_FORK)
    monkeypatch.setattr(hook.os, "_exit", REAL_EXIT)
    settings(hook_workers=3)
    monkeypatch.setattr(hook, "POLL", 0.2)
    fake_http, fake_run = hook.http, hook.subprocess.run
    def http(url, method="GET", body=None, headers=None, timeout=15):
        if method == "PUT": trace("put", url=urlparse(url).path)
        return fake_http(url, method, body, headers, timeout)
    def run(argv, **kw):
        if argv[0] != "mkvpropedit": return fake_run(argv, **kw)
        trace("edit start", path=argv[1]); real_wait(0.2)
        try:
            return fake_run(argv, **kw)
        finally:
            trace("edit end", path=argv[1])
    monkeypatch.setattr(hook, "http", http)
    monkeypatch.setattr(hook.subprocess, "run", run)
    yield env
    signal.signal(signal.SIGTERM, signal.SIG_DFL)   # coordinate() set its own


def enqueue(env, n, path, **job):
    """A job the way hook() queues one. n orders it in the queue."""
    job = dict(dict(app="radarr", event="Download", time=env["clock"][0], owner="7", file_id=None, episode_ids=None, download_id=None,
                    release=None), path=path, **job)
    name = f"{int(env['clock'][0] * 1e9) + n}-{n}.json"
    hook.store.write("INSERT INTO jobs (name, at, job) VALUES (?, ?, ?)", name, time.time(), json.dumps(job))
    return name


def films(env, k):
    """k films, movie ids 1 to k, each with its own file and Plex item. Portuguese audio plays first, so each gets an edit."""
    paths = []
    for i in range(1, k + 1):
        d = os.path.join(os.path.dirname(os.path.dirname(env["path"])), f"Film {i}")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"Film {i}.mkv"); open(p, "wb").write(b"x")
        env["movies"][f"movie/{i}"] = dict(env["movies"]["movie/7"], title=f"Film {i}", tmdbId=1000 + i, imdbId=None)
        env["plex_items"].append(plex_item(str(5000 + i), f"tmdb://{1000 + i}", p))
        paths.append(p)
    return paths


def run_worker():
    hook.worker(hook.try_lock("worker.lock"))


def claimed():
    return [n for n, in hook.store.read("SELECT name FROM jobs WHERE claimed = 1 ORDER BY name")]


def finals(env, field="outcome"):
    """{path: field} of the decision lines, the last one per path."""
    return {r["path"]: r.get(field) for r in log_lines(env) if "outcome" in r}


@pytest.mark.parametrize("workers", [1, 3])
def test_one_and_three_job_processes_give_the_same_results(pool, monkeypatch, settings, workers):
    settings(hook_workers=workers)
    paths = films(pool, 5)
    pool["files"][paths[3]] = {"container": {"properties": {"duration": 7200 * 10**9}},   # English plays first already
                               "tracks": [mk("video", "und", 1, True), mk("audio", "eng", 2, True, audio_channels=6)]}
    os.remove(paths[4])                                 # gone, and the job has no file id to ask about
    for n, p in enumerate(paths):
        enqueue(pool, n, p, owner=str(n + 1))
    run_worker()
    assert finals(pool) == {paths[0]: "edited", paths[1]: "edited", paths[2]: "edited", paths[3]: "no_change", paths[4]: "file_gone"}
    puts = traced("put")
    assert sorted(r["url"] for r in puts) == ["/library/metadata/5001/analyze", "/library/metadata/5002/analyze", "/library/metadata/5003/analyze"]
    assert {r["pid"] for r in puts} == {os.getpid()}   # only the coordinator sends an analyze, one at a time
    edits = {r["pid"] for r in traced("edit start")}
    assert (edits == {os.getpid()}) == (workers == 1)   # one worker edits in itself, three hand the jobs to job processes
    assert hook.queued() == [] and not claimed()


def test_jobs_of_one_download_check_side_by_side_and_never_edit_at_once(pool, monkeypatch):
    paths = films(pool, 4)
    real = hook.check_audio
    def check(path, j, edits, runtime=0):
        trace("check start", path=path); real_wait(0.4)
        try:
            return real(path, j, edits, runtime)
        finally:
            trace("check end", path=path)
    monkeypatch.setattr(hook, "check_audio", check)
    for n, p in enumerate(paths):
        enqueue(pool, n, p, owner=str(n + 1), download_id="pack1")
    run_worker()
    assert set(finals(pool).values()) == {"edited"}
    spans = lambda what: [(a["t"], b["t"]) for a, b in zip(sorted(traced(what + " start"), key=lambda r: r["path"]),
                                                            sorted(traced(what + " end"), key=lambda r: r["path"]))]
    overlap = lambda x, y: x[0] < y[1] and y[0] < x[1]
    checks, edits = spans("check"), spans("edit")
    assert any(overlap(a, b) for i, a in enumerate(checks) for b in checks[i + 1:])   # the reads ran side by side
    assert not any(overlap(a, b) for i, a in enumerate(edits) for b in edits[i + 1:])   # the edits never did
    assert not any(overlap(a, b) for a in edits for b in checks)   # and no read runs during an edit


def test_a_sibling_that_read_before_the_regrab_runs_again_and_is_skipped(pool, monkeypatch, settings, tmp_path):
    paths = pack(pool, monkeypatch, tmp_path, broken=(1, 2))
    settings(hook_workers=4)
    fake_write = hook.arr_write
    def arr_write(app, p, method, body=None):   # the app deletes the file, and the trace says who asked
        trace("write", call=f"{method} {p}")
        if method == "DELETE": os.remove(paths[int(p.split("/")[1])])
        return fake_write(app, p, method, body)
    monkeypatch.setattr(hook, "arr_write", arr_write)
    real = hook.check_audio
    def check(path, j, edits, runtime=0):   # both broken files are read before either job re-grabs
        trace("check", path=path)
        for _ in range(100):
            if len({r["path"] for r in traced("check")} & {paths[201], paths[202]}) == 2: break
            real_wait(0.05)
        return real(path, j, edits, runtime)
    monkeypatch.setattr(hook, "check_audio", check)
    for n, fid in enumerate((201, 202, 203, 204)):
        enqueue(pool, n, paths[fid], app="sonarr", owner="5", file_id=str(fid), episode_ids=str(fid - 100), download_id="pack1")
    run_worker()
    writes = traced("write")
    assert sorted(r["call"] for r in writes) == ["DELETE episodefile/201", "DELETE episodefile/202", "POST history/failed/900",
                                                  "PUT episode/monitor"]
    assert len({r["pid"] for r in writes}) == 1 and regrabs_counted() == 1   # one job re-grabbed the whole download
    results = finals(pool)   # the older job re-grabs, as with one worker
    assert (results[paths[201]], finals(pool, "audio")[paths[201]]["certain"]) == ("broken_audio", "the audio is silent at all 3 places checked")
    assert (results[paths[202]], finals(pool, "fault")[paths[202]]) == ("deleted_with_download", "audio")
    assert results[paths[203]] == results[paths[204]] == "edited"
    notes = [r["note"] for r in log_lines(pool) if r.get("result") == "warning"]
    assert any(n.startswith("re-planned with the file lock exclusive: ") for n in notes)


@pytest.mark.parametrize("kills, outcome, result", [(1, "edited", "edited"),
                                                    (3, "error", "error: the job process died by signal 9, 3 times, so the job is dropped")])
def test_a_job_whose_process_died_goes_back_to_the_queue(pool, monkeypatch, kills, outcome, result):
    (path,) = films(pool, 1)
    real = hook.Radarr.item
    def item(*a):
        if len(traced("killed")) < kills:
            trace("killed"); os.kill(os.getpid(), signal.SIGKILL)
        return real(*a)
    monkeypatch.setattr(hook.Radarr, "item", item)
    enqueue(pool, 0, path, owner="1")
    run_worker()
    assert finals(pool) == {path: outcome} and finals(pool, "result") == {path: result} and len(traced("killed")) == kills
    notes = [r["note"] for r in log_lines(pool) if r.get("result") == "warning"]
    assert notes[0] == "the job process died by signal 9, so the job goes back to the queue, try 2 of 3"
    assert hook.queued() == [] and not claimed()


def test_a_job_left_claimed_by_a_dead_worker_runs_again(env):
    name = enqueue(env, 0, env["path"])
    assert hook.claim(name) and claimed() == [name]
    run_worker()
    assert finals(env) == {env["path"]: "edited"} and not claimed()


def test_sigterm_requeues_the_jobs_in_flight_and_leaves_no_child(pool, monkeypatch):
    (path,) = films(pool, 1)
    pidfile = os.path.join(hook.CFG.state_dir, "ffmpeg.pid")
    def check(path, j, edits, runtime=0):   # an ffmpeg that runs long. It writes its pid, then a 30-second sleep takes its place.
        REAL_RUN(["sh", "-c", f"echo $$ > {pidfile}; exec sleep 30"])
        return None, [], []
    monkeypatch.setattr(hook, "check_audio", check)
    name = enqueue(pool, 0, path, owner="1")
    coordinator = real_fork()
    if not coordinator:   # the worker in its own process, so the SIGTERM never reaches pytest
        try:
            run_worker()
        finally:
            REAL_EXIT(0)
    for _ in range(200):
        if os.path.exists(pidfile) and open(pidfile).read().strip(): break
        real_wait(0.05)
    child = int(open(pidfile).read())
    os.kill(coordinator, signal.SIGTERM)
    for _ in range(200):
        done, status = os.waitpid(coordinator, os.WNOHANG)
        if done: break
        real_wait(0.05)
    assert done and os.waitstatus_to_exitcode(status) == 0
    for _ in range(100):
        if not os.path.exists(f"/proc/{child}"): break
        real_wait(0.05)
    assert not os.path.exists(f"/proc/{child}")   # the sleep went with its job process
    assert hook.queued() == [name] and not claimed() and not [r for r in log_lines(pool) if "outcome" in r]
    assert [r["note"] for r in log_lines(pool)] == ["stopped by SIGTERM, 0 Plex analyze requests kept for the next worker"]


def test_sigterm_waits_for_mkvpropedit(tmp_path):
    done = tmp_path / "rc"
    pid = real_fork()
    if not pid:
        code = 1
        try:
            os.setpgid(0, 0)
            signal.signal(signal.SIGTERM, hook.stopped)
            with hook.no_stop():
                done.write_text(str(REAL_RUN(["sleep", "1"]).returncode))   # mkvpropedit, which a stop signals too
            code = 0
        except SystemExit as ex:
            code = ex.code
        finally:
            REAL_EXIT(code)
    real_wait(0.3)
    os.killpg(pid, signal.SIGTERM)   # a stop that signals every process, the child included
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 128 + signal.SIGTERM   # stopped, but only after the block
    assert done.read_text() == "0"   # the child finished its write


def test_an_edit_takes_the_lock_exclusive_after_the_checks_and_rereads_the_file(env, monkeypatch):
    ops = []
    def flock(f, op):
        if f.name.endswith("/lock"): ops.append({fcntl.LOCK_SH: "shared", fcntl.LOCK_EX: "exclusive", fcntl.LOCK_UN: "unlock"}[op & ~fcntl.LOCK_NB])
        REAL_FLOCK(f, op)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    changed = []
    real = hook.video_check
    def video_check(path, again=False, **kw):   # the app touches the file while the checks read it, once
        if not changed:
            changed.append(path); os.utime(path, ns=(10**9, 10**9))
        return real(path, again, **kw)
    monkeypatch.setattr(hook, "video_check", video_check)
    name = enqueue(env, 0, env["path"])
    hook.run_job(name, [], shared=True)
    assert ops == ["shared", "unlock", "exclusive", "exclusive", "unlock"]   # the edit re-plans from scratch, exclusive throughout
    warning, editing, rec = log_lines(env)
    assert warning["note"] == "re-planned with the file lock exclusive: the file changed since the checks"
    assert rec["outcome"] == "edited" and len(env["mkvpropedit"]) == 1


def test_an_unchanged_file_is_edited_once_the_lock_is_exclusive(env, monkeypatch):
    ops = []
    def flock(f, op):
        if f.name.endswith("/lock"): ops.append(op & ~fcntl.LOCK_NB)   # a job's lock waits try with LOCK_NB
        REAL_FLOCK(f, op)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    assert ops == [fcntl.LOCK_SH, fcntl.LOCK_UN, fcntl.LOCK_EX, fcntl.LOCK_UN]   # no upgrade in place, the shared lock goes first
    assert [r.get("outcome", r["result"]) for r in log_lines(env)] == ["editing", "edited"]


def test_regrab_counts_stay_whole_under_parallel_processes(tmp_path, monkeypatch, settings):
    settings(state_dir=str(tmp_path))
    for cap, want in ((1000, 30), (20, 20)):
        settings(regrab_cap=cap)
        hook.store.drop("regrabs")
        pids = []
        for i in range(6):
            pid = real_fork()
            if not pid:
                try:
                    read = hook.regrab_times
                    hook.regrab_times = lambda app: (read(app), time.sleep(0.01))[0]   # a gap between the read and the write
                    taken = sum(hook.count_regrab("radarr") for _ in range(5))
                    (tmp_path / f"taken{i}").write_text(str(taken))
                finally:
                    REAL_EXIT(0)
            pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        assert sum(int((tmp_path / f"taken{i}").read_text()) for i in range(6)) == want   # no lost count, never past the cap
        assert len(hook.store.get("regrabs", "radarr")) == want


def test_log_lines_and_alert_markers_stay_whole_across_processes(env, monkeypatch):
    monkeypatch.setattr(hook, "post", lambda app, emb: trace("post") or "sent")
    pids = []
    for i in range(6):
        pid = real_fork()
        if not pid:
            try:
                hook.alert("radarr", "language", env["path"], 1000, {"title": "Wrong language"})
                for n in range(50):
                    hook.log(dict(source="test", writer=i, n=n, pad="x" * 30000))   # past a pipe's atomic size and the write buffer
            finally:
                REAL_EXIT(0)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    lines = log_lines(env)   # json.loads fails on an interleaved line
    assert len(lines) == 300 and len(traced("post")) == 1


STORE_HOOK = """
import os, random, sys, time
lib, ran, writes = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, lib)
from arr_media_guard import cli, logs, runner, store
def job(name, pending, shared=False, claimed=False):   # a job that writes the store many times
    path = runner.job_of(name, claimed)["path"]
    fd = os.open(ran, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    os.write(fd, (name + "\\n").encode())
    os.close(fd)
    for i in range(writes):
        store.put("stress", f"{name}|{i}", i)
        store.decided(time.time(), "radarr", path, "{}")
        time.sleep(random.random() * 0.004)
    runner.drop_job(name, claimed)
runner.run_job, logs.to_syslog = job, lambda line: None
cli.main([])
"""


def free(f):
    """Whether this process got the flock of f, which no other process holds."""
    try:
        REAL_FLOCK(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def test_real_hook_runs_and_job_processes_keep_the_store_whole(tmp_path):
    """Real hook runs of Download events, 8 at a time. Each queues its job and forks the worker, which forks a job
    process per job, HOOK_WORKERS 4 at a time. Each job writes the store 10 times. A connection that crossed a fork
    held no real lock, so another process deleted the WAL under it, see store.fork(). Each of 12 such rounds broke the
    store of 2.0.0."""
    for k in range(3):
        base, hooks = tmp_path / f"r{k}", []
        (base / "state").mkdir(parents=True)
        (base / "env").write_text(f"STATE_DIR={base}/state\nLOG={base}/log.jsonl\nHOOK_WORKERS=4\nRADARR_URL=http://127.0.0.1:9\n")
        try:
            for i in range(30):
                while sum(p.poll() is None for p in hooks) >= 8:
                    real_wait(0.01)
                env = dict(os.environ, ARR_MEDIA_GUARD_ENV=str(base / "env"), radarr_eventtype="Download", radarr_movie_id=str(i + 1),
                           radarr_moviefile_id=str(i + 1), radarr_moviefile_path=f"/media/Film {i}/Film {i}.mkv")
                hooks.append(subprocess.Popen([sys.executable, "-c", STORE_HOOK, FILES, str(base / "ran"), "10"], env=env,
                                              stdout=subprocess.DEVNULL))
            for p in hooks:
                p.wait(60)
            with open(base / "state" / "worker.lock") as f:   # the worker and its job processes hold it until the queue is empty
                assert wait_until(lambda: free(f), 60)
        finally:   # a worker that loops on a broken store
            for pid in filter(str.isdigit, os.listdir("/proc")):
                with contextlib.suppress(OSError):
                    if str(base).encode() in open(f"/proc/{pid}/cmdline", "rb").read():
                        os.kill(int(pid), signal.SIGKILL)
        with contextlib.closing(sqlite3.connect(base / "state" / "state.sqlite")) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM kv WHERE ns = 'stress'").fetchone()[0] == 300
        ran = (base / "ran").read_text().split()
        assert len(ran) == len(set(ran)) == 30   # no job lost, none run twice


def test_a_job_process_that_crashes_leaves_a_decision_line(pool, monkeypatch):
    """A job process on a host has its stderr on /dev/null, so its decision line is the only trace of a crash. The line
    masks the secrets before the cut. requeue() still counts each crash, and the third one drops the job."""
    (path,) = films(pool, 1)
    secret = hook.CFG.plex_token
    def crash(name, pending, shared=False, claimed=False):   # the token starts at character 292 of the result
        raise RuntimeError("x" * 271 + secret + "y" * 400)
    monkeypatch.setattr(hook, "run_job", crash)
    enqueue(pool, 0, path, owner="1")
    run_worker()
    lines = [r for r in log_lines(pool) if r.get("outcome") == "error"]
    crashes = [r for r in lines if r["result"].startswith("error: RuntimeError: xxx")]
    assert len(crashes) == hook.CRASH_TRIES and all(r["result"].endswith("x<PLEX_TO") and "crash" in r["trace"] for r in crashes), crashes
    assert not any(secret[:8] in json.dumps(r) for r in crashes)
    assert lines[-1]["result"] == f"error: the job process exited 1, {hook.CRASH_TRIES} times, so the job is dropped"
    assert hook.queued() == [] and not claimed()


def test_a_corrupt_store_moves_aside_and_the_import_runs_on_a_new_one(env, monkeypatch):
    """The store reads as no database. The hook writes its job as a file and starts the worker. The worker moves the
    store aside with its -wal and -shm files, starts a new one, and runs the job. One error line goes to the log and to
    syslog, and one ops embed to Discord, at most one a day."""
    state, bad = hook.CFG.state_dir, b"no database " * 400
    def break_store():
        hook.store.close()
        for end, data in (("", bad), ("-wal", b"w" * 4096), ("-shm", b"s" * 32768)):
            with open(os.path.join(state, "state.sqlite" + end), "wb") as f:
                f.write(data)
    moved = lambda: sorted(n for n in os.listdir(state) if n.startswith("state.sqlite.corrupt-"))
    posts = lambda: [b for m, u, b in env["http"] if m == "POST" and "discord" in u and b["embeds"][0]["title"] == "State store moved"]
    hook.store.db()
    break_store()
    hook.main([])
    (name,) = {n[:36] for n in moved()}
    assert moved() == [name, name + "-shm", name + "-wal"] and open(os.path.join(state, name), "rb").read() == bad
    assert [r["outcome"] for r in log_lines(env) if "schema" in r] == ["store_corrupt", "edited"]   # the job ran on the new store
    assert hook.store.corrupt() is None and os.listdir(hook.queue_dir()) == []
    errors = [r for r in log_lines(env) if r.get("outcome") == "store_corrupt"]
    assert [r["result"] for r in errors] == [f"error: the state store read as no database, so it moved to {name} and a new one started. "
                                             "0 queued jobs went to the new one, and 0 did not read. The read of its jobs stopped at a broken page."]
    assert len([x for x in env["syslog"] if "outcome=store_corrupt" in x]) == 1 and len(posts()) == 1
    real_wait(1.1)   # the next move gets a name of its own
    break_store()
    hook.check_store()
    assert len(moved()) == 6 and len([r for r in log_lines(env) if r.get("outcome") == "store_corrupt"]) == 2 and len(posts()) == 1
    os.utime(os.path.join(state, hook.STORE_ALERT), (0, 0))   # the last embed went out over a day ago
    real_wait(1.1)
    break_store()
    hook.check_store()
    assert len(moved()) == 9 and len(posts()) == 2


def test_the_jobs_of_a_store_with_a_broken_index_go_to_the_new_store(env):
    """A store with a zeroed index page still took the INSERT of each job. The worker reads the jobs from the table without
    the index and queues each one in the new store, unclaimed. A claimed copy of a queued name comes over once. A job
    that is no JSON does not read."""
    names = [enqueue(env, n, env["path"]) for n in range(3)]
    hook.store.write("INSERT INTO jobs (name, claimed, at, job) VALUES (?, 1, ?, ?)", names[0], time.time(), json.dumps({"app": "radarr"}))
    hook.store.write("INSERT INTO jobs (name, at, job) VALUES (?, ?, ?)", "9-9.json", time.time(), "{broken")
    db = hook.store.db()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    (page,) = db.execute("SELECT rootpage FROM sqlite_master WHERE name = 'sqlite_autoindex_jobs_1'").fetchone()
    size = db.execute("PRAGMA page_size").fetchone()[0]
    hook.store.close()
    with open(hook.store.path(), "r+b") as f:
        f.seek((page - 1) * size)
        f.write(bytes(size))
    assert hook.store.corrupt().startswith("failed PRAGMA quick_check: ")
    hook.check_store()
    (line,) = [r for r in log_lines(env) if r.get("outcome") == "store_corrupt"]
    assert line["result"].endswith(". 3 queued jobs went to the new one, and 1 did not read."), line["result"]
    assert hook.store.corrupt() is None and hook.queued() == sorted(names) and not claimed()


def test_a_job_that_always_crashes_stops_in_one_worker_run_when_the_store_loses_its_count(pool, monkeypatch):
    """The store drops the crash count of requeue(), so the job would come back without end. The coordinator counts the
    crashes itself and stops claiming the job after CRASH_TRIES, with one error line. The job stays queued."""
    (path,) = films(pool, 1)
    monkeypatch.setattr(hook, "run_job", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(hook, "put_job", lambda *a, **k: None)
    name = enqueue(pool, 0, path, owner="1")
    run_worker()
    crashes = [r for r in log_lines(pool) if r.get("outcome") == "error" and r["result"].startswith("error: ZeroDivisionError")]
    stops = [r for r in log_lines(pool) if r.get("result") == "error" and "crashed" in r.get("note", "")]
    assert len(crashes) == hook.CRASH_TRIES and len(stops) == 1 and hook.queued() == [name] and not claimed()


def test_a_deep_analysis_that_yields_often_is_no_crash(pool, monkeypatch):
    """A yield puts the job back and exits 0, so the crash count of coordinate() never stops it."""
    def deep(name, pending, shared=False, claimed=False):
        trace("run")
        if len(traced("run")) <= hook.CRASH_TRIES:
            hook.requeue(name)   # a yield, see deep_analysis()
        else:
            hook.drop_job(name, claimed)
    monkeypatch.setattr(hook, "run_job", deep)
    hook.store.write("INSERT INTO jobs (name, at, job) VALUES (?, ?, ?)", "deep-analysis-0123456789abcdef.json", time.time(), "{}")
    run_worker()
    assert len(traced("run")) == hook.CRASH_TRIES + 1 and hook.deep_analysis_queued() == [] and not claimed()


@pytest.mark.parametrize("case", ["repack", "header", "resub", "convert"])
def test_each_interim_result_line_carries_its_outcome_code(env, monkeypatch, tmp_path, case):
    """A repack, a header repair and a subtitle remux each log a line before anything else runs, and so does the swap
    of a conversion to a new name. Each line carries the source and the code of its step, as a decision line does."""
    rescan = lambda app, p, method, body=None: env["writes"].append((method, p, body))
    if case == "repack":
        mp4_named_mkv(env)
        monkeypatch.setattr(hook, "arr_write", rescan)
    elif case == "header":
        header_issue(env, monkeypatch)
        monkeypatch.setattr(hook, "arr_write", rescan)
    elif case == "resub":
        removal_film(env, monkeypatch, tmp_path)
    else:
        mp4_import(env, monkeypatch)
    hook.main([])
    result, code = {"repack": ("repacked", "repacked"), "header": ("header repaired", "header_repaired"),
                    "resub": ("subtitles remuxed", "subtitles_remuxed"), "convert": ("converting", "converting")}[case]
    (line,) = [r for r in log_lines(env) if r.get("result") == result and "schema" not in r]
    assert (line["source"], line["outcome"]) == ("hook", code)


def test_a_failed_fork_puts_the_job_back_and_the_next_pass_runs_it(pool, monkeypatch):
    (path,) = films(pool, 1)
    forks = []
    def fork():
        forks.append(1)
        if len(forks) == 1: raise BlockingIOError(11, "Resource temporarily unavailable")
        return real_fork()
    monkeypatch.setattr(hook.os, "fork", fork)
    enqueue(pool, 0, path, owner="1")
    run_worker()
    assert {r["path"]: r["outcome"] for r in log_lines(pool) if "outcome" in r} == {path: "edited"} and len(forks) == 2 and not claimed()
    assert [r["note"] for r in log_lines(pool) if r.get("result") == "warning"] == [
        "no job process: BlockingIOError: [Errno 11] Resource temporarily unavailable"]
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == set()   # SIGTERM is not left blocked


def test_a_job_process_passes_back_its_plex_analyze_and_the_tmdb_pause(env):
    p = hook.plex_job("radarr", "hook", "Film A (1979)", env["path"], {"guids": ["tmdb://90001"], "title": "Film A", "show": False}, "ab")
    down = dict(until=env["clock"][0] + 600, code="tmdb_unavailable", why="timed out", answered=0.0)
    pending = []
    hook.finished("1-1.json", 0, json.dumps({"plex": [p], "down": down}).encode(), pending)
    assert pending == [json.loads(json.dumps(p))] and hook.arr_meta.DOWN == down   # the next job processes skip TMDB too
    hook.finished("1-2.json", 0, json.dumps({"plex": [], "down": dict(down, until=0.0)}).encode(), pending)
    assert hook.arr_meta.DOWN == down and len(pending) == 1   # an older pause never shortens the current one


def cross_pack(pool, monkeypatch, tmp_path, workers, silent, first=None):
    """Files 201 and 202 of one download, queued in that order, with HOOK_WORKERS workers. silent(path, reader) says
    whether ffmpeg hears silence in path while the job of the file reader runs. With job processes, both files are
    read before either job takes its exclusive step, and first, a path, finishes its checks first. Returns the paths."""
    paths = pack(pool, monkeypatch, tmp_path, broken=())
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, hook_workers=workers))
    mine = []
    real_run_job = hook.run_job
    def run_job(name, pending, shared=False, claimed=False):
        mine[:] = [hook.job_of(name, claimed)["path"]]
        return real_run_job(name, pending, shared=shared, claimed=claimed)
    monkeypatch.setattr(hook, "run_job", run_job)
    real_run = hook.subprocess.run
    def run(argv, **k):
        if argv[0] == "ffmpeg":
            out = SILENCE if silent(argv[argv.index("-i") + 1], mine[0]) else "[x] [info] n_samples: 1922128\n[x] [info] max_volume: -4.0 dB\n"
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": out})()
        if argv[0] == "mkvpropedit": trace("edit", path=argv[1])
        return real_run(argv, **k)
    monkeypatch.setattr(hook.subprocess, "run", run)
    fake_write = hook.arr_write
    def arr_write(app, p, method, body=None):
        trace("write", call=f"{method} {p}")
        if method == "DELETE": os.remove(paths[int(p.split("/")[1])])
        return fake_write(app, p, method, body)
    monkeypatch.setattr(hook, "arr_write", arr_write)
    real_check = hook.check_audio
    own = {paths[201], paths[202]}
    def check(path, j, edits, runtime=0):
        if workers > 1 and path == mine[0]:   # a job's own check, not a re-grab's probe
            trace("check", path=path)
            for _ in range(100):   # both files are read before either job takes its exclusive step
                if {r["path"] for r in traced("check")} >= own: break
                real_wait(0.05)
            if first and path != first:
                for _ in range(100):
                    if traced("checked"): break
                    real_wait(0.05)
            try:
                return real_check(path, j, edits, runtime)
            finally:
                trace("checked", path=path)
        return real_check(path, j, edits, runtime)
    monkeypatch.setattr(hook, "check_audio", check)
    real_exclusive = hook.exclusive
    def exclusive(lock, path, *a):   # the other file reaches its exclusive step late, so only the settled marker holds first back
        if workers > 1 and first and path != first: real_wait(1.5)
        return real_exclusive(lock, path, *a)
    monkeypatch.setattr(hook, "exclusive", exclusive)
    for n, fid in enumerate((201, 202)):
        enqueue(pool, n, paths[fid], app="sonarr", owner="5", file_id=str(fid), episode_ids=str(fid - 100), download_id="pack1")
    run_worker()
    return paths


@pytest.mark.parametrize("workers", [1, 3])
def test_a_download_ends_the_same_with_one_or_three_job_processes(pool, monkeypatch, tmp_path, workers):
    """Both files hear silence in their own job's checks, and a tone when the other job's re-grab
    probes them. One worker deletes 201, and 202 is then clean in the unit and gets its edit. Three job processes must
    end the same way, even when 202 finishes its checks first: it waits for 201's re-grab, sees the unit changed, and
    runs again."""
    first = os.path.join(str(tmp_path), "media", "Show", "Season 1", "Show - s01e02.mkv")
    paths = cross_pack(pool, monkeypatch, tmp_path, workers, lambda path, reader: path == reader, first=first)
    assert sorted(r["call"] for r in traced("write") if r["call"].startswith("DELETE")) == ["DELETE episodefile/201"]
    unit = hook.store.get("unit", "sonarr|pack1")
    assert unit["deleted"] == [201] and 202 in unit["clean"] and 201 not in unit["clean"]
    results = finals(pool)   # the re-grab's probes of the other files add their own lines
    assert (results[paths[201]], results[paths[202]]) == ("broken_audio", "edited")
    assert finals(pool, "audio")[paths[201]]["certain"] == "the audio is silent at all 3 places checked"


@pytest.mark.parametrize("workers", [1, 3])
def test_a_younger_file_the_older_regrab_deletes_is_never_edited_first(pool, monkeypatch, tmp_path, workers):
    """201 is silent to every reader. 202 hears a tone in its own checks and silence in 201's re-grab. One worker
    deletes both and never edits 202. With job processes 202 finishes its checks first and wants its edit, but 201 is
    not settled, so 202 waits, finds its file gone, and is skipped."""
    first = os.path.join(str(tmp_path), "media", "Show", "Season 1", "Show - s01e02.mkv")
    paths = cross_pack(pool, monkeypatch, tmp_path, workers,
                       lambda path, reader: path.endswith("s01e01.mkv") or (path.endswith("s01e02.mkv") and reader != path), first=first)
    assert sorted(r["call"] for r in traced("write") if r["call"].startswith("DELETE")) == ["DELETE episodefile/201", "DELETE episodefile/202"]
    assert [r["path"] for r in traced("edit")] == []
    results = finals(pool)
    assert (results[paths[201]], finals(pool, "audio")[paths[201]]["certain"]) == ("broken_audio", "the audio is silent at all 3 places checked")
    assert (results[paths[202]], finals(pool, "fault")[paths[202]]) == ("deleted_with_download", "audio")


def wait_until(done, seconds=10):
    """Wait in real time until done() holds, seconds at most."""
    for _ in range(int(seconds / 0.05)):
        if done(): return True
        real_wait(0.05)
    return False


def mp4_movies(pool, monkeypatch, k):
    """k MP4 imports, movies 1 to k, each in its own folder with file id 10 + i. The fake Radarr takes a ManualImport as
    in mp4_import(), and the trace names each write. Returns the MP4 paths."""
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, convert=True))
    refreshes(pool, monkeypatch)
    pool["probe"] = copy.deepcopy(NOT_MATROSKA["probe"])
    for i, t in enumerate(pool["probe"]["tracks"]):
        t["id"] = i
    paths = []
    for i in range(1, k + 1):
        d = os.path.join(os.path.dirname(os.path.dirname(pool["path"])), f"Film {i}")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"Film {i}.mp4"); open(p, "wb").write(b"x" * 1000)
        old = {"id": 10 + i, "path": p, "quality": {"quality": {"id": 3, "name": "WEBDL-1080p"}, "revision": {"version": 1}},
               "languages": [{"id": 1, "name": "English"}], "releaseGroup": "GRP", "sceneName": f"Film.{i}.2000.1080p.WEB-DL-GRP", "indexerFlags": 0,
               "customFormatScore": 25}
        pool["movies"][f"movie/{i}"] = dict(pool["movies"]["movie/7"], title=f"Film {i}", tmdbId=1000 + i, monitored=True, path=d,
                                            movieFile={k: v for k, v in old.items() if k != "customFormatScore"})
        pool["movies"][f"moviefile/{10 + i}"] = old
        paths.append(p)

    def arr_write(app, p, method, body=None):
        trace("write", call=f"{method} {p}")
        if p == "command" and body["name"] == "ManualImport":
            f = body["files"][0]
            pool["movies"][f'movie/{f["movieId"]}']["movieFile"] = {"id": 20 + f["movieId"], "path": f["path"]}
        return {"id": 1, "status": "completed"} if p == "command" else [dict(body[0], customFormatScore=25)] if p == "moviefile/bulk" else None
    monkeypatch.setattr(hook, "arr_write", arr_write)
    return paths


def edit_lock(monkeypatch):
    """Trace "edit unlocked" when mkvpropedit starts while no process holds the file lock exclusive."""
    run = hook.subprocess.run
    def probe(argv, **kw):
        if argv[0] == "mkvpropedit":
            with open(os.path.join(hook.CFG.state_dir, "lock")) as f:
                with contextlib.suppress(BlockingIOError):
                    REAL_FLOCK(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    trace("edit unlocked", path=argv[1])
        return run(argv, **kw)
    monkeypatch.setattr(hook.subprocess, "run", probe)


def test_hook_conversions_remux_side_by_side_and_swap_alone(pool, monkeypatch):
    """Three MP4 imports in three job processes. Their remuxes run at once under the shared lock. Each swap takes the
    lock exclusive, so it waits until no other remux runs."""
    paths = mp4_movies(pool, monkeypatch, 3)
    run = hook.subprocess.run
    def remux(argv, **kw):   # each remux starts once all three run, and the first to end is the shortest
        if "mkvmerge" not in argv or "-o" not in argv: return run(argv, **kw)
        n = int(os.path.basename(argv[-1]).split()[1].split(".")[0])
        trace("remux start", n=n)
        wait_until(lambda: len(traced("remux start")) == 3, 3)
        real_wait(0.3 * n)
        try:
            return run(argv, **kw)
        finally:
            trace("remux end", n=n)
    monkeypatch.setattr(hook.subprocess, "run", remux)
    hide = hook.hide_extras
    monkeypatch.setattr(hook, "hide_extras", lambda extras: trace("swap") or hide(extras))   # inside the swap, under the lock
    edit_lock(monkeypatch)
    for n, p in enumerate(paths):
        enqueue(pool, n, p, owner=str(n + 1), file_id=str(11 + n))
    run_worker()
    assert {r["path"]: r["reasons"][0] for r in log_lines(pool) if "schema" in r} == {p[:-4] + ".mkv": "repacked" for p in paths}
    assert len(traced("edit start")) == 3 and traced("edit unlocked") == []   # each flag edit after a conversion holds the lock exclusive
    assert all(os.path.exists(p[:-4] + ".mkv") and not os.path.exists(p) for p in paths)
    spans = {r["n"]: [r["t"], None] for r in traced("remux start")}
    for r in traced("remux end"):
        spans[r["n"]][1] = r["t"]
    spans = list(spans.values())
    assert len(spans) == 3 and all(a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i + 1:])   # the remuxes ran at once
    swaps = [r["t"] for r in traced("swap")]
    assert len(swaps) == 3 and min(swaps) > max(e for _, e in spans)   # no swap while any remux ran


def pack_with_mp4(pool, monkeypatch, tmp_path, settings):
    """The pack of pack(), with 201 an MP4 that converts and 202 broken, and HOOK_WORKERS 2. The fake Sonarr answers
    what a conversion asks, and lists the new file as 201 after its ManualImport. The trace names each write. Returns
    the pack's paths, 201 the MP4."""
    paths = pack(pool, monkeypatch, tmp_path, broken=(2,))
    settings(convert=True, hook_workers=2)
    mp4 = paths[201][:-4] + ".mp4"
    os.replace(paths[201], mp4)
    paths[201] = mp4
    pool["files"][mp4] = copy.deepcopy(NOT_MATROSKA["probe"])
    for i, t in enumerate(pool["files"][mp4]["tracks"]):
        t["id"] = i
    home = os.path.dirname(os.path.dirname(mp4))
    pack_arr = hook.arr
    def arr(app, p):
        if p == "series/5": return {"title": "Show", "originalLanguage": {"name": "English"}, "path": home}
        if p.startswith("parse?"):
            n = parse_qs(p.split("?", 1)[1])["title"][0].lower().partition("s01e")[2][:2]
            return {"episodes": [{"id": 100 + int(n)}] if n.isdigit() else []}
        if p.startswith("episodefile?seriesId="): return [{"id": f, "path": x} for f, x in paths.items()]
        if p.startswith("history/series?"): return []
        if p.startswith("qualityprofile/"): return {}
        return pack_arr(app, p)
    monkeypatch.setattr(hook, "arr", arr)
    fake_write = hook.arr_write
    def arr_write(app, p, method, body=None):
        trace("write", call=f"{method} {p}")
        if method == "DELETE": os.remove(paths[int(p.split("/")[1])])
        if p == "command" and body.get("name") == "ManualImport": paths[201] = body["files"][0]["path"]
        return fake_write(app, p, method, body)
    monkeypatch.setattr(hook, "arr_write", arr_write)
    return paths


def test_a_hook_conversion_refuses_its_swap_after_a_regrab_deleted_its_original(pool, monkeypatch, tmp_path, settings):
    """202 of a pack has broken audio, and 201 is an MP4 that converts. Both jobs run at once. 202's re-grab takes
    the lock between 201's remux and its swap, and the probe of 201 hears silence, so the app deletes 201. 201's swap
    then finds its original gone and changes nothing. 201's job runs again and is skipped."""
    paths = pack_with_mp4(pool, monkeypatch, tmp_path, settings)
    mp4, run = paths[201], hook.subprocess.run
    def silent(argv, **kw):   # the re-grab's probe of 201 hears silence
        if argv[0] == "ffmpeg" and argv[argv.index("-i") + 1] == mp4:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": SILENCE})()
        return run(argv, **kw)
    monkeypatch.setattr(hook.subprocess, "run", silent)
    swap_lock, wait_turn = hook.swap_lock, hook.wait_turn
    def late_swap(lock):   # 202's re-grab takes the lock first, as it can when both wait for it
        REAL_FLOCK(lock, fcntl.LOCK_UN)
        trace("at swap")
        assert wait_until(lambda: any(r["call"] == "DELETE episodefile/201" for r in traced("write")))
        return swap_lock(lock)
    monkeypatch.setattr(hook, "swap_lock", late_swap)
    def turn(name, job):   # 202 takes its exclusive step only once 201's remux is done, so 201 always converts first
        if job["file_id"] == "202":
            assert wait_until(lambda: traced("at swap"), 30)
        return wait_turn(name, job)
    monkeypatch.setattr(hook, "wait_turn", turn)
    enqueue(pool, 0, paths[202], app="sonarr", owner="5", file_id="202", episode_ids="102", download_id="pack1")
    enqueue(pool, 1, mp4, app="sonarr", owner="5", file_id="201", episode_ids="101", download_id="pack1")
    run_worker()
    results = finals(pool)
    assert (results[paths[202]], results[mp4], finals(pool, "fault")[mp4]) == ("broken_audio", "deleted_with_download", "audio")
    assert sorted(r["call"] for r in traced("write")) == ["DELETE episodefile/201", "DELETE episodefile/202", "POST history/failed/900",
                                                          "PUT episode/monitor"]   # no ManualImport: 201 never swapped
    assert sorted(os.listdir(os.path.dirname(mp4))) == sorted(os.path.basename(paths[f]) for f in range(203, 211))   # no new file, temp file or held name
    notes = [r["note"] for r in log_lines(pool) if r.get("result") == "warning"]
    assert len(traced("at swap")) == 1 and "re-planned with the file lock exclusive: the file is gone since the checks" in notes
    assert open(os.path.join(hook.CFG.state_dir, "convert-sonarr.txt")).read().split("\t")[1] == "repack_source_changed"   # before the re-plan


def test_a_converted_job_edits_only_after_the_older_jobs_of_its_download(pool, monkeypatch, tmp_path, settings):
    """202's job is older and re-grabs. It waits before its exclusive step until 201 has converted. 201 then waits for
    202's re-grab before its own checks and its flag edit, as with one worker."""
    paths = pack_with_mp4(pool, monkeypatch, tmp_path, settings)
    mp4, convert, wait_turn = paths[201], hook.convert, hook.wait_turn
    monkeypatch.setattr(hook, "convert", lambda *a, **k: (lambda out: trace("converted") or out)(convert(*a, **k)))
    def turn(name, job):
        if job["file_id"] == "202":   # the older job lets 201 convert first, and is then slow to take its lock
            assert wait_until(lambda: traced("converted"))
            real_wait(0.5)
        return wait_turn(name, job)
    monkeypatch.setattr(hook, "wait_turn", turn)
    enqueue(pool, 0, paths[202], app="sonarr", owner="5", file_id="202", episode_ids="102", download_id="pack1")
    enqueue(pool, 1, mp4, app="sonarr", owner="5", file_id="201", episode_ids="101", download_id="pack1")
    run_worker()
    mkv = mp4[:-4] + ".mkv"
    assert (finals(pool)[paths[202]], finals(pool)[mkv]) == ("broken_audio", "edited") and os.path.exists(mkv) and not os.path.exists(mp4)
    deleted = [r["t"] for r in traced("write") if r["call"] == "DELETE episodefile/202"]
    assert len(deleted) == 1 and [r["t"] > deleted[0] for r in traced("edit start")] == [True]


def twins(pool, monkeypatch):
    """Two jobs of one MP4 import in two job processes, A queued first. B reaches its temp file only after A synced
    it. The trace names each job's ManualImport, its convert() result and the job B that found the temp file there.
    Returns (mp4, mkv, me), me[0] the job of the process."""
    mp4, mkv = mp4_import(pool, monkeypatch, sidecars=())
    refreshes(pool, monkeypatch)
    me = []
    real_run_job = hook.run_job
    def run_job(name, pending, shared=False, claimed=False):
        me[:] = ["A" if name.endswith("-0.json") else "B"]
        return real_run_job(name, pending, shared=shared, claimed=claimed)
    monkeypatch.setattr(hook, "run_job", run_job)
    sync, new_tmp, convert, busy, write = hook.Swap.sync, hook.new_tmp, hook.convert, hook.busy_tmp, hook.arr_write
    monkeypatch.setattr(hook.Swap, "sync", lambda self: sync(self) or trace("synced", who=me[0]))
    def b_after_a(tmp, excl=False):
        assert me[0] == "A" or wait_until(lambda: traced("synced"))
        return new_tmp(tmp, excl)
    monkeypatch.setattr(hook, "new_tmp", b_after_a)
    monkeypatch.setattr(hook, "busy_tmp", lambda lock, tmp: trace("busy", who=me[0]) or busy(lock, tmp))
    monkeypatch.setattr(hook, "convert", lambda *a, **k: (lambda out: trace("converted", who=me[0], code=out[0]) or out)(convert(*a, **k)))
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: trace("write", who=me[0], command=(body or {}).get("name")
                                                                                    if p == "command" else None) or write(app, p, method, body))
    enqueue(pool, 0, mp4, file_id="11")
    enqueue(pool, 1, mp4, file_id="11")
    return mp4, mkv, me


@pytest.mark.parametrize("stopped", [False, True])
def test_a_twin_job_of_one_file_converts_after_the_first(pool, monkeypatch, stopped):
    """Two jobs of one MP4 import run at once. B finds A's temp file, lets the lock go and waits. A swaps, and B then
    runs again under the exclusive lock and finds the file gone. A SIGTERM to B during A's swap stops B, and A's swap
    still ends well, because B never wrote or removed the temp file."""
    mp4, mkv, me = twins(pool, monkeypatch)
    swap_lock, hide = hook.swap_lock, hook.hide_extras
    def swap(lock):   # A swaps once B waits for the temp file, and late, so a B that did not wait would take the lock first
        assert wait_until(lambda: traced("busy"))
        REAL_FLOCK(lock, fcntl.LOCK_UN)
        real_wait(0.5)
        return swap_lock(lock)
    monkeypatch.setattr(hook, "swap_lock", swap)
    def stop(extras):   # inside A's swap, before the link and the rename
        if stopped:
            (b,) = [r["pid"] for r in traced("busy")]
            os.kill(b, signal.SIGTERM)
            assert wait_until(lambda: not os.path.exists(f"/proc/{b}") or open(f"/proc/{b}/stat").read().rsplit(")", 1)[1].split()[0] == "Z")
        return hide(extras)
    monkeypatch.setattr(hook, "hide_extras", stop)
    run_worker()
    assert {r["who"]: r["code"] for r in traced("converted")} == {"A": "repacked"}   # B's convert() raised Replan
    assert [r["who"] for r in traced("write") if r["command"] == "ManualImport"] == ["A"]
    assert finals(pool)[mp4] == "file_gone" and finals(pool)[mkv] == "edited" and os.path.exists(mkv) and not os.path.exists(mp4)
    assert not [r for r in log_lines(pool) if r.get("outcome") == "repack_failed"]
    notes = [r["note"] for r in log_lines(pool) if r.get("result") == "warning"]
    assert ("re-planned with the file lock exclusive: another conversion of the file wrote its temp file" in notes) != stopped
    assert sorted(os.listdir(os.path.dirname(mp4))) == [os.path.basename(mkv)]   # no temp file, no held name


def test_a_stale_temp_file_costs_one_wait_and_is_written_over(pool, monkeypatch):
    """A killed job left the temp file of an MP4 import. The job finds it under the shared lock, waits SWAP_POLL
    seconds for it to go, and runs again under the exclusive lock, which writes over it and converts."""
    mp4, mkv = mp4_import(pool, monkeypatch, sidecars=())
    refreshes(pool, monkeypatch)
    monkeypatch.setattr(hook, "SWAP_POLL", 0)   # the fake clock never moves in a real wait
    tmp = hook.repack_tmp(mp4)
    os.makedirs(os.path.dirname(tmp))
    open(tmp, "wb").write(b"half a remux")
    enqueue(pool, 0, mp4, file_id="11")
    run_worker()
    assert finals(pool)[mkv] == "edited" and open(mkv, "rb").read() == b"MKV!" + b"x" * 1000 and not os.path.exists(mp4)
    notes = [r["note"] for r in log_lines(pool) if r.get("result") == "warning"]
    assert notes == ["re-planned with the file lock exclusive: another conversion of the file wrote its temp file"]
    assert sorted(os.listdir(os.path.dirname(mp4))) == [os.path.basename(mkv)]


def test_a_swap_refuses_a_temp_file_written_after_its_checks(env):
    """The identity check behind the exclusive create: a temp file that another write changed after sync() never
    takes the original's place."""
    tmp = hook.repack_tmp(env["path"])
    hook.new_tmp(tmp)
    open(tmp, "wb").write(b"MKV!" + b"x" * 1000)
    sw = hook.Swap(env["path"], os.stat(env["path"]), tmp, {})
    try:
        sw.sync()
        sw.check("changed", False)   # the temp file the checks read
        open(tmp, "wb").write(b"MKV!" + b"y" * 500)
        with pytest.raises(RuntimeError, match="the temp file changed after its checks"):
            sw.check("changed", False)
    finally:
        sw.close(None)
    assert not os.path.exists(tmp)


def test_sigterm_after_an_edit_finishes_the_job_and_keeps_its_plex_analyze(pool, monkeypatch):
    (path,) = films(pool, 1)
    run = hook.subprocess.run
    def edit(argv, **kw):   # the stop reaches the coordinator while mkvpropedit writes
        if argv[0] == "mkvpropedit":
            os.kill(os.getppid(), signal.SIGTERM); real_wait(1.0)
        return run(argv, **kw)
    monkeypatch.setattr(hook.subprocess, "run", edit)
    enqueue(pool, 0, path, owner="1")
    coordinator = real_fork()
    if not coordinator:
        try:
            run_worker()
        finally:
            REAL_EXIT(0)
    assert os.waitstatus_to_exitcode(os.waitpid(coordinator, 0)[1]) == 0
    assert finals(pool) == {path: "edited"} and hook.queued() == [] and not claimed()   # the job finished and was not re-queued
    kept = hook.store.get("plex", "pending")
    assert [k["path"] for k in kept] == [path] and traced("put") == []
    run_worker()   # the next worker sends the kept analyze
    assert [r["url"] for r in traced("put")] == ["/library/metadata/5001/analyze"]
    assert hook.store.get("plex", "pending") is None


@pytest.mark.parametrize("value, workers", [("4", 4), ("x", 1), ("0", 1), ("-2", 1), ("", 1)])
def test_a_bad_hook_workers_value_falls_back_to_one_and_says_so(tmp_path, monkeypatch, value, workers):
    (tmp_path / "queue").mkdir()
    (tmp_path / "env").write_text(f"HOOK_WORKERS='{value}'\nSTATE_DIR='{tmp_path}'\nLOG='{tmp_path}/log.jsonl'\n")
    monkeypatch.setenv("ARR_MEDIA_GUARD_ENV", str(tmp_path / "env"))
    m = amg.load("arr_media_guard_env")
    assert m.CFG.hook_workers == workers
    m.worker(m.try_lock("worker.lock"))   # an empty queue, so it only starts and exits
    notes = [json.loads(line)["note"] for line in open(tmp_path / "log.jsonl")] if os.path.exists(tmp_path / "log.jsonl") else []
    assert notes == ([] if value in ("4", "") else [f"HOOK_WORKERS {value!r} is not a whole number of 1 or more, so 1 runs"])


def test_a_stop_during_the_post_frees_the_alert_marker(env, monkeypatch):
    def stopped(app, emb):
        raise SystemExit(143)
    monkeypatch.setattr(hook, "post", stopped)
    with pytest.raises(SystemExit):
        hook.alert("radarr", "language", env["path"], 1000, {"title": "Wrong language"})
    monkeypatch.setattr(hook, "post", lambda app, emb: "sent")
    assert hook.alert("radarr", "language", env["path"], 1000, {"title": "Wrong language"}) == "sent"


# --- header repair -------------------------------------------------------------------------------
# A lossless mkvmerge remux writes the Segment duration, the Cues and the Segment size from the streams (docs/design.md, "Header
# repair"). The generated files carry the shapes of real files.

def el(i, data):
    """An EBML element with an 8-byte size."""
    return i.to_bytes((i.bit_length() + 7) // 8, "big") + (1 << 56 | len(data)).to_bytes(8, "big") + data


def seek_entry(i, pos):
    return el(0x4DBB, el(0x53AB, i.to_bytes(4, "big")) + el(0x53AC, pos.to_bytes(8, "big")))


def set_duration(path, secs):
    """Write secs into the Segment Info Duration in place, as a header that says 13:23:33 for 1:54:33."""
    D, b = hook.arr_decide, bytearray(open(path, "rb").read(1 << 16))
    ds, _ = D.segment_start(b)
    info = D.element(b, ds + D.front_seeks(b, ds)[0x1549A966][0])
    (_, d, s), = [c for c in D.children(b, info[1], info[1] + info[2]) if c[0] == 0x4489]
    b[d:d + s] = struct.pack(">d" if s == 8 else ">f", secs * 1000)   # TimestampScale 1 ms
    with open(path, "r+b") as f:
        f.write(b)


def seekhead_at_end(src, dst):
    """The front SeekHead lists only a main SeekHead at the end, and the file ends where that one
    would start, after the hook's own failed mkvpropedit run. Every Cluster and the Cues are intact."""
    D, b = hook.arr_decide, bytearray(open(src, "rb").read())
    ds, seg = D.segment_start(b)
    seeks = D.front_seeks(b, ds)
    info = seeks[0x1549A966][0]   # the SeekHead and its Void fill the bytes up to the Info
    main = el(D.SEEKHEAD, b"".join(seek_entry(i, p[0]) for i, p in seeks.items()))
    head = el(D.SEEKHEAD, seek_entry(D.SEEKHEAD, len(b) - ds)); pad = info - len(head) - 9
    assert b[ds - 8] == 1   # mkvmerge writes an 8-byte Segment size
    b[ds:ds + info] = head + b"\xec" + (1 << 56 | pad).to_bytes(8, "big") + bytes(pad)
    b[ds - 8:ds] = (1 << 56 | (seg + len(main))).to_bytes(8, "big")
    open(dst, "wb").write(b)
    return len(main)


@pytest.fixture(scope="module")
def mkvs(tmp_path_factory):
    """Matroska files from mkvmerge: a clean one, a wrong duration, no Cues, a main SeekHead past the end with its
    track order, a wrong duration with zero-filled regions, a subtitle event that runs 57 minutes past the 2-minute
    video and one that starts 48 minutes after it."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d = tmp_path_factory.mktemp("mkvs")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=120", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=120", "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "aac", str(d / "src.mkv")],
                   check=True)
    mux = lambda out, *args: subprocess.run(["mkvmerge", "-q", "-o", str(d / out), *args], check=True)
    mux("good.mkv", str(d / "src.mkv"))
    shutil.copy(d / "good.mkv", d / "long.mkv"); set_duration(d / "long.mkv", 48213)
    mux("nocues.mkv", "--cues", "0:none", "--cues", "1:none", str(d / "good.mkv"))
    (d / "short.srt").write_text("1\n00:00:10,000 --> 00:00:12,000\nA line\n")   # subtitles, audio, video in that order
    mux("ordered.mkv", str(d / "short.srt"), str(d / "good.mkv"), "--track-order", "0:0,1:1,1:0")
    seekhead_at_end(d / "ordered.mkv", d / "seekhead.mkv")
    shutil.copy(d / "long.mkv", d / "zeroed.mkv")
    with open(d / "zeroed.mkv", "r+b") as f:   # 2 MiB of zeros at 30 and at 60 percent, an unfinished download
        for share in (0.3, 0.6):
            f.seek(int(os.path.getsize(d / "zeroed.mkv") * share)); f.write(bytes(2 << 20))
    (d / "late.srt").write_text("1\n00:00:10,000 --> 00:59:00,000\nA line that never ends\n\n2\n00:00:20,000 --> 00:00:21,000\nA line\n")
    mux("subtitle.mkv", str(d / "good.mkv"), "--language", "0:spa", "--track-name", "0:Signs", "--forced-display-flag", "0:1",
        "--default-track-flag", "0:0", str(d / "late.srt"))   # one event runs to 59:00
    (d / "stray.srt").write_text("1\n00:00:10,000 --> 00:00:12,000\nA line\n\n2\n00:50:00,000 --> 00:50:01,000\nA stray line\n")
    mux("stray.mkv", str(d / "good.mkv"), str(d / "stray.srt"))   # an event starts 48 minutes after the end
    srt = lambda events: "\n\n".join(f"{k + 1}\n{stamp(a)} --> {stamp(b)}\n{text}" for k, (a, b, text) in enumerate(events)) + "\n"
    (d / "eng.srt").write_text(srt([(10 + 4 * k, 12 + 4 * k, f"English {k}") for k in range(24)]))
    (d / "ita.srt").write_text(srt([(7.8 * k, 7.8 * k + 2, f"Italiano {k}") for k in range(24)]))   # timed for a cut 1.5x as long
    mux("othercut.mkv", str(d / "good.mkv"), "--language", "0:eng", "--track-name", "0:English", "--default-track-flag", "0:0", str(d / "eng.srt"),
        "--language", "0:ita", "--track-name", "0:Italiano", "--default-track-flag", "0:1", str(d / "ita.srt"))
    (d / "onelate.srt").write_text(srt([(0.2 * k, 0.2 * k + 0.1, f"Line {k}") for k in range(500)] + [(125, 300, "One late line")]))
    mux("onelate.mkv", str(d / "good.mkv"), "--language", "0:eng", str(d / "onelate.srt"))   # 1 of 501 late
    (d / "forced.srt").write_text(srt([(20 * k, 20 * k + 2, f"Sign {k}") for k in range(5)] + [(125, 300, "One stray sign")]))
    mux("forced.mkv", str(d / "good.mkv"), "--forced-display-flag", "0:1", str(d / "forced.srt"))   # 1 of 6 late: a trim
    # A film cut at 60 percent: 3 of its 5 minutes, and a good SubRip track that runs to the full length
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=180", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=180", "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "aac", str(d / "cutsrc.mkv")],
                   check=True)
    (d / "full.srt").write_text(srt([(6 * k, 6 * k + 3, f"Line {k}") for k in range(50)]))
    mux("cut.mkv", str(d / "cutsrc.mkv"), "--language", "0:eng", str(d / "full.srt"))
    # A shorter copy written over an older, larger one without a truncate. Zeros start at the
    # Segment end, then the Clusters of the old copy follow.
    old = (d / "cutsrc.mkv").read_bytes()
    junk = bytes(3 << 19) + old[old.find(hook.arr_decide.CLUSTER.to_bytes(4, "big")):]
    (d / "tail.mkv").write_bytes((d / "good.mkv").read_bytes() + junk)
    (d / "tail_long.mkv").write_bytes((d / "long.mkv").read_bytes() + junk)   # and a header duration no stream reaches
    # The write of the shorter copy stopped at 60 percent. The last 40 percent of its Segment holds the old
    # copy, a larger encode, so the Clusters before the Segment end come from early in the old timeline.
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=180", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=180", "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-crf", "2", "-c:a", "aac",
                    str(d / "oldsrc.mkv")], check=True)
    mux("old.mkv", str(d / "oldsrc.mkv"))
    good, old = (d / "good.mkv").read_bytes(), (d / "old.mkv").read_bytes()
    (d / "interrupted.mkv").write_bytes(good[:len(good) * 6 // 10] + old[len(good) * 6 // 10:])
    (d / "joined.mkv").write_bytes(good + good)   # two files joined with cat, and ffmpeg plays both
    os.remove(d / "oldsrc.mkv")
    os.remove(d / "cutsrc.mkv")
    return d


def stamp(t):
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{round(t % 1 * 1000):03d}"


@pytest.mark.parametrize("name, repairable", [("good.mkv", None), ("long.mkv", True), ("nocues.mkv", True), ("seekhead.mkv", True),
                                              ("seekhead-no-log", False), ("zeroed.mkv", False), ("subtitle.mkv", True), ("stray.mkv", True),
                                              ("othercut.mkv", True), ("onelate.mkv", True), ("forced.mkv", True), ("tail.mkv", True),
                                              ("tail_long.mkv", False), ("interrupted.mkv", False), ("joined.mkv", False)])
def test_header_verdicts_on_generated_files(mkvs, tmp_path, monkeypatch, settings, name, repairable):
    settings(log=str(tmp_path / "log.jsonl"))
    path = str(mkvs / name.replace("-no-log", ".mkv"))
    if name == "seekhead.mkv":   # the hook's own edit of it failed: an editing line with no edited line after it
        hook.log(dict(path=path, result="editing"))
    certain, doubts, fields = hook.video_check(path)
    h = fields["header"]
    assert h.get("repairable") is repairable, (certain, doubts, h)
    at = [w["at"] for w in (fields.get("windows") or {}).get("list", [])]
    if name == "good.mkv":   # the common case reads the start and the Cues, never the end
        assert h["issue"] == [] and h["cues"] is True and h["video"] is None and h["read"] < 1 << 20 and doubts == []
    elif name == "long.mkv":   # the windows sit in the streams, never at 13:23:33 times a share
        assert h["issue"] == ["the header says 13:23:33, but the streams end at 2:00"] and at == [12, 60, 102] and h["end"] == 120.023
    elif name == "nocues.mkv":   # each window reads the file from its start, and all three decode clean
        assert h["issue"] == ["no usable Cues index: no SeekHead lists the Cues"] and at == [12, 60, 102] and h["windows_clean"]
    elif name == "seekhead.mkv":   # a header issue, no longer a doubt
        assert doubts == [] and h["issue"] == [
            f"the Segment size promises {h['short']} bytes past the end of the file after the hook's own edit of it failed",
            "no usable Cues index: the SeekHead that lists the Cues sits past the end of the file"]
    elif name == "seekhead-no-log":   # without the hook's failed edit, bytes past the end are real damage
        assert doubts == [f"the file may be missing {h['short']} bytes at its end"] and h["blocked"] == doubts
    elif name == "zeroed.mkv":   # real damage wins
        assert certain.startswith("the file has blank gaps at") and certain in h["blocked"] and h["issue"]
    elif name == "tail.mkv":   # the zeros and the old Clusters past the Segment end are no damage, and the windows sit in the Segment
        tail = os.path.getsize(path) - os.path.getsize(mkvs / "good.mkv")
        assert (h["tail"], h["issue"], h["blocked"]) == (tail, [f"the file holds {tail} bytes past the end of its Matroska Segment"], [])
        assert (certain, doubts, fields["zeros"]["hits"], at, h["video"]) == (None, [], [], [12, 60, 102], 120.0)
    elif name in ("tail_long.mkv", "interrupted.mkv"):   # the Segment may be cut, so no remux takes the tail, and the doubt alerts
        assert h["not_whole"].startswith(f"the file has {h['tail']} bytes of extra data at its end, and the video and audio stop before its "
                                         f"stated length of {hook.arr_meta.hms(h['duration'])}") and h["not_whole"] in h["blocked"]
        assert certain is None and doubts == [h["not_whole"]] and h["blocked"].count(h["not_whole"]) == 1, (doubts, h["blocked"])
    elif name == "joined.mkv":   # the second file plays, so the remux must not drop it
        assert h["blocked"] == ["the bytes past the Segment end start another Matroska file"] and (certain, doubts) == (None, [])
    else:   # a subtitle event sets the duration, and a remux alone keeps it. A late track is trimmed, or removed when 10
        # percent or more of its lines start after the end.
        end = {"subtitle.mkv": "59:00", "stray.mkv": "50:01", "othercut.mkv": "3:01", "onelate.mkv": "5:00", "forced.mkv": "5:00"}[name]
        assert h["issue"] == [f"a subtitle event runs to {end}, past the video and the audio at 2:00"] and at == [12, 60, 102]
        # stray.mkv: 1 of 2 is late, under the floor of 2 late lines, so a trim. forced.mkv: 1 of 6.
        plan = {"subtitle.mkv": ([2], []), "stray.mkv": ([2], []), "othercut.mkv": ([], [3]), "onelate.mkv": ([2], []), "forced.mkv": ([2], [])}[name]
        assert (h["trim"], h["remove"], h["expect"], h["streams"], h["blocked"]) == (*plan, 120.023, 120.023, []), h
        lines = {"subtitle.mkv": {2: [2, 0]}, "stray.mkv": {2: [2, 1]}, "othercut.mkv": {2: [24, 0], 3: [24, 8]}, "onelate.mkv": {2: [501, 1]},
                 "forced.mkv": {2: [6, 1]}}
        assert h["sublines"] == lines[name]


@pytest.mark.parametrize("name, end, issue", [
    ("subtitle.mkv", 3540.0, "a subtitle event runs to 59:00, past the video and the audio at 2:00"),
    ("long.mkv", 120.023, "the header says 13:23:33, but the streams end at 2:00")])
def test_a_long_subtitle_event_before_the_last_clusters_is_found(mkvs, tmp_path, monkeypatch, settings, name, end, issue):
    """One subtitle event that lasts 13 hours may set a 13:23:33 duration and still sit before the last clusters.
    A remux keeps that duration. A demux of the subtitle packets finds the event, and only a header with no such
    event is a header a remux repairs."""
    settings(log=str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(hook, "HEADER_TAIL", (256 << 10,))   # the event at 10 s sits before the last 256 KiB
    h = hook.header_of(str(mkvs / name))
    assert (h.get("subtitles"), h["end"], h["issue"][0]) == ({2: 3540.0} if name == "subtitle.mkv" else None, end, issue), h
    assert h.get("trim") == ([2] if name == "subtitle.mkv" else None) and h["blocked"] == []
    monkeypatch.setattr(hook, "subtitle_ends", lambda *a, **k: None)   # ffprobe failed or ran out of time
    assert name == "long.mkv" or "the subtitle events were not read: no time, no remux can follow, or ffprobe failed" in hook.header_of(str(mkvs / name))["blocked"]


@pytest.mark.parametrize("name", ["long.mkv", "nocues.mkv", "seekhead.mkv"])
def test_a_header_repair_writes_the_header_from_the_streams(mkvs, tmp_path, monkeypatch, settings, name):
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "Movie (2000)" / name; path.parent.mkdir(); shutil.copy(mkvs / name, path); os.chmod(path, 0o640)
    if name == "seekhead.mkv":
        hook.log(dict(path=str(path), result="editing"))
    j = hook.mkvmerge(str(path)); hp = hook.header_of(str(path), j)
    _, result, info = hook.repack(str(path), j, os.stat(path), True, hp)
    assert result == "header repaired" and os.listdir(path.parent) == [name], (result, info)
    after = hook.header_of(str(path))
    assert after["issue"] == [] and after["cues"] is True and after["short"] == 0 and abs(after["duration"] - hp["end"]) <= 1
    assert info["new_duration"] == after["duration"] and info["new_cues"] is True and [w["frames"] for w in info["windows"]] == [120, 120, 120]
    new = hook.mkvmerge(str(path))   # the same tracks in the same order, with the same UIDs
    assert os.stat(path).st_mode & 0o777 == 0o640 and hook.track_list(new) == hook.track_list(j)
    assert [t["properties"]["uid"] for t in new["tracks"]] == [t["properties"]["uid"] for t in j["tracks"]]
    assert name != "seekhead.mkv" or [t[0] for t in hook.track_list(new)] == ["subtitles", "audio", "video"]


def test_a_header_repair_runs_in_a_conversion_worker_thread(mkvs, tmp_path, monkeypatch, settings):
    """A backfill's conversion workers run process() in threads, and signal.signal() works in the main thread only."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "long.mkv"; shutil.copy(mkvs / "long.mkv", path)
    j = hook.mkvmerge(str(path))
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        _, result, info = pool.submit(hook.repack, str(path), j, os.stat(path), True, hook.header_of(str(path), j)).result()
    assert result == "header repaired", result


@pytest.mark.parametrize("knob, fault", [("REPACK_SIZE", "the size changed"), ("REPAIR_END", "the new header says 120.023 seconds")])
def test_a_header_repair_that_fails_its_checks_keeps_the_original(mkvs, tmp_path, monkeypatch, settings, knob, fault):
    settings(log=str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(hook if knob == "REPACK_SIZE" else hook.arr_decide, knob, -1)   # every check of this kind fails
    path = tmp_path / "long.mkv"; shutil.copy(mkvs / "long.mkv", path); before = path.read_bytes()
    j = hook.mkvmerge(str(path))
    _, result, info = hook.repack(str(path), j, os.stat(path), True, hook.header_of(str(path), j))
    assert result.startswith(f"header repair failed: {fault}") and path.read_bytes() == before, result
    assert sorted(os.listdir(tmp_path)) == ["log.jsonl", "long.mkv"] or os.listdir(tmp_path) == ["long.mkv"]


def test_a_tail_past_the_segment_end_goes_with_the_remux(mkvs, tmp_path, monkeypatch, settings):
    """A file may hold bytes past the Segment end.
    mkvmerge reads only the Segment, so the remux drops the tail, and the checks of a header repair hold."""
    settings(log=str(tmp_path / "log.jsonl"))
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".arr-media-guard-originals"))
    path = tmp_path / "Show K" / "tail.mkv"; path.parent.mkdir(); shutil.copy(mkvs / "tail.mkv", path)
    before, j = os.stat(path), hook.mkvmerge(str(path))
    certain, doubts, fields = hook.video_check(str(path), j=j)
    h = fields["header"]
    assert (certain, doubts, h["repairable"]) == (None, [], True), h
    code, result, info = hook.repack(str(path), j, before, True, h)
    assert result == "tail removed" and os.listdir(path.parent) == ["tail.mkv"], (result, info)
    assert code == "tail_removed" in hook.REPAIRED
    after, new = hook.header_of(str(path)), hook.mkvmerge(str(path))
    segment = before.st_size - h["tail"]   # the size check compares with the Segment, never the whole file
    assert (after["issue"], after["tail"], after["short"]) == ([], 0, 0) and abs(info["new_size"] - segment) <= 0.03 * segment < h["tail"]
    assert info["frames"] == 2880 and [w["frames"] for w in info["windows"]] == [120, 120, 120] and info["warnings"] is None
    assert [t["properties"]["uid"] for t in new["tracks"]] == [t["properties"]["uid"] for t in j["tracks"]]
    assert new["container"]["properties"]["segment_uid"] == j["container"]["properties"]["segment_uid"]
    assert os.stat(info["kept"]).st_ino == before.st_ino   # the original with its tail stays for KEEP_DAYS


@pytest.mark.parametrize("name, repairable", [("long.mkv", False), ("nocues.mkv", True)])
def test_a_read_capped_window_is_no_doubt(mkvs, tmp_path, monkeypatch, settings, name, repairable):
    """Rule F. In a file that indexes its video once, each window reads to the 512 MiB cap with no error, and a full
    decode finds none. Such a window is no doubt. It still keeps a header with usable Cues from a repair, and in a
    file with no usable Cues it never did."""
    settings(log=str(tmp_path / "log.jsonl"))
    capped = dict(CLEAN_WINDOW, frames=0, empty=True, ran=False, stopped="read over 512 MiB", read=512 << 20, took=9.0)
    monkeypatch.setattr(hook, "window", lambda path, start, secs: dict(capped, at=round(start)))
    certain, doubts, fields = hook.video_check(str(mkvs / name))
    h = fields["header"]
    assert (certain, doubts, h["repairable"]) == (None, [], repairable), h
    stops = [f"the video check at {at} read over 512 MiB, so the file may have no usable index" for at in ("0:12", "1:00", "1:42")]
    assert h["blocked"] == ([] if repairable else stops)


def test_the_hook_logs_a_removed_tail_as_its_own_repair(env, monkeypatch):
    header_issue(env, monkeypatch, result=("tail_removed", "tail removed"))
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: env["writes"].append((method, p, body)))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["reasons"][0], rec["header_repair"]["code"]) == ("edited", "tail_removed", "tail_removed"), rec
    assert [r["result"] for r in log_lines(env)][0] == "tail removed" and rec["header_repair"]["rescan"] == "sent"


HEADER_INTERRUPT = INTERRUPT.replace('h.convert("radarr", sys.argv[2], {"container": {"type": "MP4/QuickTime"}, "tracks": []}, '
                                     'os.stat(sys.argv[2]), True)',
                                     'h.repack(sys.argv[2], {"container": {"type": "Matroska"}, "tracks": []}, os.stat(sys.argv[2]), True, '
                                     '{"issue": ["x"], "duration": 48213.0, "end": 120.0, "expect": 120.0, "cues": True, "video": 120.0})')


@pytest.mark.parametrize("tool", ["fake", "mkvmerge"])
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupted_header_repair_leaves_no_temp_file(mkvs, tmp_path, tool, sig):
    """The header repair runs in repack(), so Ctrl+C or SIGTERM during the remux or its checks removes the temp file and
    keeps the original, the same as a repack."""
    assert HEADER_INTERRUPT != INTERRUPT
    interrupted(tmp_path, HEADER_INTERRUPT, mkvs / "long.mkv", tool, sig)


HEADER_HP = {"duration": 48213.5, "cues": True, "cue_end": 6873.4, "short": 0, "failed_edit": False, "video": 6873.4, "audio": 6873.4,
             "end": 6898.0, "read": 1, "issue": ["the header says 13:23:33, but the streams end at 1:54:58"], "blocked": []}


def header_issue(env, monkeypatch, repairable=True, result=("header_repaired", "header repaired"), doubts=()):
    """A file whose header has an issue until a repair ran. The video check before the repair says whether it may run,
    and repack() records each call. result is what an applied repack() returns, as (code, text). Returns the record."""
    calls = {"probes": 0, "checks": [], "repairs": [], "done": False}

    def header_of(path, j=None):
        calls["probes"] += 1
        return None if calls["done"] else copy.deepcopy(HEADER_HP)

    def video_check(path, again=False, j=None, hp=None):
        calls["checks"].append(hp)
        h = dict(hp or {}, repairable=repairable, blocked=[] if repairable else ["zero-filled regions at 18 of 256 offsets"])
        return None, list(doubts), {"fault": None, "header": h, "zeros": {"hits": []}}

    def repack(path, j, st, apply, hp=None):
        calls["repairs"].append(apply)
        calls["done"] = calls["done"] or (apply and result[1] == "header repaired")
        code, got = result if apply else ("would_repair_header", "would repair header: " + "; ".join(hp["issue"]))
        return code, got, dict(old_size=st.st_size, old_duration=hp["duration"], end=hp["end"], **({"new_size": st.st_size} if got == "header repaired" else {}))
    for name, fake in (("header_of", header_of), ("video_check", video_check), ("repack", repack)):
        monkeypatch.setattr(hook, name, fake)
    return calls


def test_the_hook_repairs_a_header_then_edits_the_new_file(env, monkeypatch):
    calls = header_issue(env, monkeypatch)
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: env["writes"].append((method, p, body)) or env["events"].append("rescan"))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["reasons"][0], rec["header_repair"]["code"]) == ("edited", "header_repaired", "header_repaired"), rec
    assert [r.get("outcome", r["result"]) for r in log_lines(env)][:3] == ["header_repaired", "editing", "edited"]   # the record of the repair comes first
    assert calls["repairs"] == [True] and len(calls["checks"]) == 1   # the check before the repair stands for the same streams after it
    assert rec["video"]["header"]["repairable"] is True and rec["header_repair"]["rescan"] == "sent"
    assert env["writes"] == [("POST", "command", {"name": "RescanMovie", "movieId": 7})]
    assert env["events"].index("mkvpropedit") < env["events"].index("rescan")
    assert analyzes(env) == ["/library/metadata/7101/analyze"]


def test_a_failed_header_repair_alerts_and_still_edits(env, monkeypatch):
    header_issue(env, monkeypatch, result=("header_repair_failed", "header repair failed: the size changed from 1000 to 2000 bytes"))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["reasons"][0]) == ("edited", "header_repair_failed") and env["writes"] == []
    assert rec["findings"] == [{"kind": "header", "why": "the size changed from 1000 to 2000 bytes"}]
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1


def test_a_header_issue_with_real_damage_is_never_repaired(env, monkeypatch):
    calls = header_issue(env, monkeypatch, repairable=False, doubts=["the file has a blank gap at 30% of its length"])
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [] and len(calls["checks"]) == 1 and rec["reasons"][0] == "header_not_repaired"
    assert rec["header_repair"]["result"] == "not repaired: zero-filled regions at 18 of 256 offsets"
    assert rec["findings"] == [{"kind": "video", "doubts": ["the file has a blank gap at 30% of its length"]}]


def test_header_repair_switched_off_never_repairs(env, monkeypatch, settings):
    calls = header_issue(env, monkeypatch)
    settings(header_repair=False)
    hook.main([])
    assert calls["probes"] == 0 and calls["repairs"] == [] and calls["checks"] == [None] and "header_repair" not in decided(env)


def test_a_job_process_repairs_a_header_with_the_lock_exclusive(env, monkeypatch):
    calls = header_issue(env, monkeypatch)
    ops = []
    def flock(f, op):
        if f.name.endswith("/lock"): ops.append({fcntl.LOCK_SH: "shared", fcntl.LOCK_EX: "exclusive", fcntl.LOCK_UN: "unlock"}[op & ~fcntl.LOCK_NB])
        REAL_FLOCK(f, op)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    assert ops[:2] == ["shared", "exclusive"] and calls["repairs"] == [True]   # the repair runs only in the exclusive re-run
    notes = [r["note"] for r in log_lines(env) if r.get("result") == "warning"]
    assert notes == ["re-planned with the file lock exclusive: the file needs a header repair"]


def test_a_backfill_reports_repairs_and_both_audits_count_them(env, monkeypatch, tmp_path, capsys):
    calls = header_issue(env, monkeypatch)
    env["probe"] = {"container": {"properties": {"duration": 7200 * 10**9}},   # English plays first, so no flag edit
                    "tracks": [mk("video", "und", 1, True), mk("audio", "eng", 2, True, audio_channels=6), mk("subtitles", "eng", 3, False)]}
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 70, "movieId": 7, "path": env["path"],
                                                                              "mediaInfo": {"audioStreamCount": 2}})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    plans = tmp_path / "plans.jsonl"
    hook.main(["--backfill", "radarr", "--plan-out", str(plans)])
    rec = decided(env)
    assert (rec["outcome"], rec["reasons"][0], calls["repairs"]) == ("no_change", "would_repair_header", [False])
    assert (rec["header_repair"]["code"], rec["header_repair"]["issue"]) == ("would_repair_header", HEADER_HP["issue"])
    hook.main(["--audit", "radarr", "--plan-from", str(plans)])
    assert "1 would get a new header from a remux." in capsys.readouterr().out
    hook.main(["--backfill", "radarr", "--apply", "--plan-from", str(plans)])   # a plan with no edit is kept for its repair
    assert calls["repairs"] == [False, True] and env["writes"] == [("POST", "command", {"name": "RescanMovie", "movieId": 7})]
    assert analyzes(env) == ["/library/metadata/7101/analyze"]
    hook.main(["--audit", "radarr", "--since", "24h"])
    out = capsys.readouterr().out
    assert "1 got a new header from a remux." in out and "1 header repaired (Film A (1979))" in out


def test_a_video_scan_lists_a_header_issue_and_never_repairs_it(env, monkeypatch, tmp_path):
    scan_library(monkeypatch, tmp_path, 2)
    def check(path, dur, again=False, hp=None):
        h = dict(HEADER_HP, repairable=True) if path.endswith("m1.mkv") else {"skipped": "not Matroska, or no Segment size"}
        return None, [], {"fault": None, "header": h}
    monkeypatch.setattr(hook, "check_video", check)
    monkeypatch.setattr(hook, "repack", lambda *a, **k: pytest.fail("a scan never repairs"))
    hook.main(["--backfill", "radarr", "--check-video", "--workers", "1"])
    lines = open(os.path.join(hook.CFG.state_dir, "video-scan-radarr.txt")).read().splitlines()
    assert lines == [f"HEADER\tMovie 1 (2000)\tthe header says 13:23:33, but the streams end at 1:54:58. A remux repairs it\t{tmp_path}/media/m1.mkv"]


def test_windows_follow_the_video_track_duration_never_the_segment_duration(env, monkeypatch, tmp_path):
    """The Segment says 2:47:13, the video track's DURATION tag 1:52:17. A window at 85 percent of the Segment would
    fall past the end of the video."""
    app = "mkvmerge v86.0 ('Name D') 64-bit"
    tagged = {"container": {"type": "Matroska", "properties": {"duration": 10033200000000, "writing_application": app}},
              "tracks": [{"type": "video", "properties": {"tag_duration": "01:52:17.310000000", "tag__statistics_writing_app": app}}]}
    assert hook.video_seconds(tagged) == 6737.31
    assert hook.video_seconds(tagged, {"video": 6736.0}) == 6736.0   # the stream end read from the file wins
    stale = copy.deepcopy(tagged); stale["tracks"][0]["properties"]["tag__statistics_writing_app"] = "mkvmerge v40.0"
    assert hook.video_seconds(stale) is None   # a tag another muxer copied says nothing about this file
    f = tmp_path / "Film F.mkv"; f.write_bytes(noise(1 << 20))
    env["files"][str(f)] = tagged
    monkeypatch.setattr(hook, "ffprobe_duration", lambda p: 10033.2)
    hook.video_check(str(f))
    assert [at for p, at in env["windows"]] == [674, 3369, 5727]


# --- parallel job processes, more cases ----------------------------------------------------------

def test_a_claimed_older_job_with_no_process_is_not_waited_for(env):
    older, younger, job = "100-1.json", "200-2.json", {"download_id": "pack1"}
    for n in (older, younger):
        hook.store.write("INSERT INTO jobs (name, claimed, at, job) VALUES (?, 1, 0, ?)", n, json.dumps(job))
    dead = subprocess.Popen(["true"]); dead.wait()
    hook.store.write("UPDATE jobs SET pid = ? WHERE name = ?", dead.pid, older)   # its requeue failed, or its coordinator died
    hook.wait_turn(younger, job)   # returns at once, no hour of waiting
    hook.wait_turn(younger, job)
    notes = [r["note"] for r in log_lines(env) if r.get("result") == "warning"]
    assert notes == [f"the older job {older} of this download is claimed, but no job process runs it, so this job goes on without waiting for it"] * 2
    hook.store.write("UPDATE jobs SET pid = ? WHERE name = ?", os.getpid(), older)   # a live process: wait until it settles
    threading.Timer(0.3, lambda: hook.settle(older)).start()
    started = time.perf_counter()
    hook.wait_turn(younger, job)
    assert time.perf_counter() - started >= 0.3 and len([r for r in log_lines(env) if r.get("result") == "warning"]) == 2


def test_a_job_process_of_another_user_counts_as_alive(env, monkeypatch):
    """The kernel refuses a signal to another user's process with EPERM. That process runs, so its job is waited for."""
    hook.store.write("INSERT INTO jobs (name, claimed, at, pid, job) VALUES ('100-1.json', 1, 0, 4242, '{}')")
    def kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(hook.os, "kill", kill)
    assert hook.job_alive("100-1.json") is True


@pytest.mark.parametrize("unit, outcome", [(True, "deleted_with_download"), (False, "file_gone")])
def test_a_rerun_reads_its_download_after_the_lock(env, monkeypatch, unit, outcome):
    """A younger job's re-grab deleted this file while the re-run waited for its lock. The re-run reads the unit under
    the lock and is skipped, never an error on the missing file."""
    name = enqueue(env, 0, env["path"], file_id="11", download_id="pack1")
    real = hook.locked
    def locked(shared=False, wait=None):
        if unit:
            hook.store.put("unit", "radarr|pack1", {"time": env["clock"][0], "failed": True, "deleted": [11], "clean": [], "kind": "audio"})
        os.remove(env["path"])
        return real(shared, wait or hook.DEADLINE)
    monkeypatch.setattr(hook, "locked", locked)
    hook.run_job(name, [])
    rec = decided(env)
    assert rec["outcome"] == outcome and "trace" not in rec, rec
    assert not unit or rec["fault"] == "audio"
    assert unit or rec["note"] == "the file went while the job waited for the lock"


def test_the_wrong_content_regrab_waits_its_turn(env, monkeypatch, settings):
    wrong_film(env, monkeypatch)
    settings(regrab={"audio", "video", "content"})
    order, real = [], hook.gated
    monkeypatch.setattr(hook, "wait_turn", lambda name, job: order.append("turn"))
    monkeypatch.setattr(hook, "gated", lambda f, op, wait=hook.DEADLINE: order.append("exclusive" if op == fcntl.LOCK_EX else "shared")
                        or real(f, op, wait))
    hook.run_job(enqueue(env, 0, env["path"], file_id="11", download_id="a1b2c3d4", release=WRONG_RELEASE), [], shared=True)
    assert order[-2:] == ["turn", "exclusive"] and order.count("turn") == order.count("exclusive"), order
    assert env["writes"][0] == ("DELETE", "moviefile/11", None)


def test_a_wrong_content_regrab_that_waited_out_its_turn_skips_the_regrab(env, monkeypatch, settings):
    """wait_turn() returns once LOCK_WAIT passed, with an older job of the download still unsettled. The lock wait
    before the re-grab then gives up. The metadata checks record it, and nothing is deleted."""
    wrong_film(env, monkeypatch)
    settings(regrab={"audio", "video", "content"})
    monkeypatch.setattr(hook, "wait_turn", lambda name, job: env["clock"].__setitem__(0, env["clock"][0] + hook.LOCK_WAIT))
    hook.run_job(enqueue(env, 0, env["path"], file_id="11", download_id="a1b2c3d4", release=WRONG_RELEASE), [], shared=True)
    rec = decided(env)
    assert rec["meta_error"] == f"OutOfTime: stopped after {hook.BUDGET} seconds" and env["writes"] == [] and "regrab" not in rec, rec


def test_a_long_wait_for_the_lock_before_a_wrong_content_regrab_leaves_it_its_time(env, monkeypatch, settings):
    """The re-grab reads the app's history right after its lock wait, under a new BUDGET."""
    wrong_film(env, monkeypatch)
    settings(regrab={"audio", "video", "content"})
    real = hook.arr
    monkeypatch.setattr(hook, "arr", lambda app, p: hook.DEADLINE.check() or real(app, p))   # as http() does
    def flock(f, o):   # the first exclusive try finds the lock held for an hour less a second
        if f.name.endswith("/lock") and o == fcntl.LOCK_EX | fcntl.LOCK_NB and not env.get("waited"):
            env["waited"] = True
            env["clock"][0] += hook.LOCK_WAIT - 1
            raise BlockingIOError
        REAL_FLOCK(f, o)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    hook.run_job(enqueue(env, 0, env["path"], file_id="11", download_id="a1b2c3d4", release=WRONG_RELEASE), [], shared=True)
    rec = decided(env)
    assert rec["outcome"] == "wrong_content" and env["waited"] and env["writes"][0] == ("DELETE", "moviefile/11", None), rec.get("meta_error")


# --- real damage in a file with no usable Cues, the subtitle trim, the kept original ---------------------

@pytest.fixture(scope="module")
def damaged(tmp_path_factory):
    """80 s of noisy video at 2 Mbit/s with no Cues, then three copies damaged the ways real files were. Every window
    reads the file from its start, because nothing says where a cluster is. One encoder thread and a fixed noise seed
    give the same file on every machine, since x264's output changes with the thread count."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d = tmp_path_factory.mktemp("damaged")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=80,noise=alls=40:allf=t:all_seed=1", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=80", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "2M", "-g", "48", "-threads", "1", "-c:a", "aac",
                    str(d / "src.mkv")], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(d / "nocues.mkv"), "--cues", "0:none", "--cues", "1:none", str(d / "src.mkv")], check=True)
    size = os.path.getsize(d / "nocues.mkv")
    for name, at, data in (("garbage.mkv", 0.4, noise(2 << 20)), ("hole.mkv", 0.4, bytes(160 << 10))):
        shutil.copy(d / "nocues.mkv", d / name)
        with open(d / name, "r+b") as f:
            f.seek(int(size * at)); f.write(data)
    # the SeekHead points where no Cues sit, and a cluster is damaged
    shutil.copy(d / "src.mkv", d / "badcues.mkv")
    D, b = hook.arr_decide, bytearray(open(d / "badcues.mkv", "rb").read(1 << 16))
    ds, _ = D.segment_start(b)
    head = D.element(b, ds)
    for i, dd, ss in D.children(b, head[1], head[1] + head[2]):
        got = {ci: (cd, cs) for ci, cd, cs in D.children(b, dd, dd + ss)}
        if i == D.SEEK and int.from_bytes(b[got[0x53AB][0]:sum(got[0x53AB])], "big") == D.CUES:
            pos, n = got[0x53AC]
            b[pos:pos + n] = (int.from_bytes(b[pos:pos + n], "big") // 2).to_bytes(n, "big")   # now inside a cluster
    with open(d / "badcues.mkv", "r+b") as f:
        f.write(b)
        f.seek(int(os.path.getsize(d / "badcues.mkv") * 0.4)); f.write(noise(2 << 20))
    os.remove(d / "src.mkv")
    return d


@pytest.mark.parametrize("name", ["garbage.mkv", "hole.mkv", "badcues.mkv"])
def test_real_damage_in_a_file_with_no_usable_cues_is_never_repaired(damaged, tmp_path, monkeypatch, settings, name):
    """The windows of a file with no usable Cues read it from the start, so damage before a
    window shows as container errors, and 2 bad windows are certain. A remux forced past that gate still fails its own
    checks: mkvmerge warns when it resyncs past garbage, and a zero hole it drops with no warning costs frames."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = str(damaged / name)
    certain, doubts, fields = hook.video_check(path)
    h = fields["header"]
    assert h["issue"][0].startswith("no usable Cues index") and h["repairable"] is False, (certain, doubts, h)
    assert certain and certain.startswith("the video is broken at 2 of 3 places checked") and certain in h["blocked"], certain
    work = tmp_path / "work"; work.mkdir(); copy_ = work / name; shutil.copy(path, copy_); before = copy_.read_bytes()
    j = hook.mkvmerge(str(copy_))
    _, result, info = hook.repack(str(copy_), j, os.stat(copy_), True, dict(hook.header_of(str(copy_), j), windows_clean=False))
    assert result.startswith("header repair failed: ") and copy_.read_bytes() == before and os.listdir(work) == [name], result
    assert ("mkvmerge exited" in result) if name != "hole.mkv" else ("video frames, but the video end at" in result or "mkvmerge exited" in result)


def test_a_clean_file_with_no_cues_still_repairs(damaged, tmp_path, monkeypatch, settings):
    """Every window reads from the start and decodes clean, and the new file holds every frame."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "nocues.mkv"; shutil.copy(damaged / "nocues.mkv", path)
    certain, doubts, fields = hook.video_check(str(path))
    assert fields["header"]["repairable"] and fields["header"]["windows_clean"], (certain, doubts, fields["header"])
    _, result, info = hook.repack(str(path), hook.mkvmerge(str(path)), os.stat(path), True, fields["header"])
    assert result == "header repaired" and info["frames"] == 1920 and info["warnings"] is None, (result, info)


def test_a_read_cap_stop_with_no_error_is_no_sign_of_damage(mkvs, tmp_path, monkeypatch, settings):
    """Every window of a large file with no index may read over 512 MiB and stop. That window is exempt from the
    repair gate only when it logged no error on the way, and only in a file with no usable Cues."""
    settings(log=str(tmp_path / "log.jsonl"))
    capped = dict(CLEAN_WINDOW, frames=0, empty=True, ran=False, stopped="read over 512 MiB")
    for w, cues, repairable in ((capped, "no SeekHead lists the Cues", True), (dict(capped, errors=1), "no SeekHead lists the Cues", False),
                                (capped, True, False)):
        monkeypatch.setattr(hook, "window", lambda p, start, secs, w=w: dict(w, at=round(start), took=0.1, read=1))
        hp = dict(hook.header_of(str(mkvs / "nocues.mkv")), cues=cues)
        h = hook.check_video(str(mkvs / "nocues.mkv"), 120.0, hp=hp)[2]["header"]
        assert h["repairable"] is repairable and h["windows_clean"] is False, (w, cues, h)


def test_a_subtitle_trim_cuts_late_lines_and_keeps_everything_else(mkvs, tmp_path, monkeypatch, settings):
    """Every SubRip line that runs past the real end ends there, and one that starts after
    it goes. A track with 10 percent or more of its lines starting after the end is timed for another cut and is removed.
    A track that is wrong in general is better gone. Video and audio are untouched. A trimmed track keeps its
    place, language, name and flags, with a new UID. Every other track keeps its UID."""
    settings(log=str(tmp_path / "log.jsonl"))
    cases = (("subtitle.mkv", "subtitles trimmed", {2: {"events": 2, "cut": 1, "dropped": 0}}, {}),
             ("onelate.mkv", "subtitles trimmed", {2: {"events": 501, "cut": 0, "dropped": 1}}, {}),
             ("stray.mkv", "subtitles trimmed", {2: {"events": 2, "cut": 0, "dropped": 1}}, {}),   # 1 late line is under the floor
             ("othercut.mkv", "subtitles removed", {}, {3: {"language": "ita", "name": "Italiano", "codec": "SubRip/SRT", "lines": 24, "late": 8}}))
    for name, result_want, trimmed, removed in cases:
        path = tmp_path / name; shutil.copy(mkvs / name, path)
        j = hook.mkvmerge(str(path))
        h = hook.video_check(str(path))[2]["header"]
        _, _, plan = hook.repack(str(path), j, os.stat(path), False, h)
        assert plan["removed"] == {i: dict(r, lines=r["lines"], late=r["late"]) for i, r in removed.items()}, plan   # a dry run names them
        _, result, info = hook.repack(str(path), j, os.stat(path), True, h)
        assert (result, info["trimmed"], info["removed"], info["new_duration"]) == (result_want, trimmed, removed, 120.023), (result, info)
        new = hook.mkvmerge(str(path))
        kept = [t for t in j["tracks"] if t["id"] not in removed]
        assert hook.arr_decide.duration(new) == 120.023 and hook.track_list(new) == hook.track_list({"tracks": kept})
        for a, b in zip(kept, new["tracks"]):
            assert all(a["properties"].get(k, d) == b["properties"].get(k, d) for k, d in hook.KEEP_PROPS)
            assert (a["properties"]["uid"] == b["properties"]["uid"]) == (a["id"] not in trimmed)
        if name in ("subtitle.mkv", "onelate.mkv"):
            subprocess.run(["mkvextract", str(path), "tracks", f"2:{tmp_path / 'out.srt'}"], check=True, capture_output=True)
            srt = (tmp_path / "out.srt").read_text().lstrip("\ufeff")   # mkvextract writes a BOM
            if name == "subtitle.mkv":
                assert srt == "1\n00:00:10,000 --> 00:02:00,023\nA line that never ends\n\n2\n00:00:20,000 --> 00:00:21,000\nA line\n\n", srt
            else:
                assert srt.count(" --> ") == 500 and "One late line" not in srt
    if True:   # a bitmap subtitle cannot be cut: the file stays, with one alert
        j = hook.mkvmerge(str(mkvs / "subtitle.mkv"))
        j["tracks"][2]["properties"]["codec_id"] = "S_HDMV/PGS"
        hp = hook.header_probe(str(mkvs / "subtitle.mkv"), j)
        assert hp["unfixable"] == [{"track": "s1", "codec": "S_HDMV/PGS", "end": 3540.0, "streams": 120.023}] and "trim" not in hp and "remove" not in hp
        assert hp["blocked"] == ["subtitle track 2 (S_HDMV/PGS) runs past the end, and only a SubRip track can be trimmed"]


def test_an_unfixable_subtitle_overrun_alerts_once_and_changes_nothing(env, monkeypatch):
    calls = header_issue(env, monkeypatch, repairable=False)
    hp = dict(HEADER_HP, issue=["a subtitle event runs to 13:23:33, past the video and the audio at 1:54:33"],
              unfixable=[{"track": "s1", "codec": "S_HDMV/PGS", "end": 48213.4, "streams": 6873.9}])
    monkeypatch.setattr(hook, "header_of", lambda p, j=None: copy.deepcopy(hp))
    hook.main([])
    rec = decided(env)
    assert calls["repairs"] == [] and rec["reasons"][0] == "subtitle_overrun_unfixable" and rec["outcome"] == "edited"
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1
    assert rec["findings"] == [{"kind": "subtitle", "issue": hp["issue"], "tracks": hp["unfixable"]}]


@pytest.mark.parametrize("case", ["kept", "pruned", "no place", "link fails", "killed", "killed at the rename", "trim fails"])
def test_a_repack_keeps_the_original_for_a_week(mkvs, tmp_path, monkeypatch, settings, case):
    """Every repack kind hard-links the original into KEEP_DIR at the top of its mount before the rename, so the
    path is never missing and the original stays on the same file system. The nightly audit and each keep drop the
    originals older than KEEP_DAYS. No place to keep it skips the repack before any write, and a failed link keeps the
    original where it was. A kill before the rename removes the kept link. A failed trim still marks the
    time limit as off, so process() re-arms it."""
    settings(log=str(tmp_path / "log.jsonl"))
    settings(keep_days=7)
    lib = tmp_path / "movies" / "Movie (2000)"; lib.mkdir(parents=True)
    path = lib / "Movie (2000).mkv"; shutil.copy(mkvs / "long.mkv", path)
    root = tmp_path / hook.CFG.keep_dir
    monkeypatch.setattr(hook, "originals_root", lambda p: str(root))
    old = root / "20000101T000000Z" / "movies" / "x.mkv"; old.parent.mkdir(parents=True); old.write_bytes(b"old")
    before, st = path.read_bytes(), os.stat(path)
    j = hook.mkvmerge(str(path)); hp = hook.header_of(str(path), j)
    if case == "no place":
        monkeypatch.setattr(hook, "keepable", lambda p, s: f"{root} is on another file system")
    if case == "link fails":
        monkeypatch.setattr(hook.os, "link", lambda a, b: (_ for _ in ()).throw(OSError(5, "Input/output error")))   # no refusal, see LINK_REFUSED
    if case == "killed":
        monkeypatch.setattr(hook.os, "replace", lambda a, b: (_ for _ in ()).throw(SystemExit(143)))
        with pytest.raises(SystemExit):
            hook.repack(str(path), j, st, True, hp)
        assert path.read_bytes() == before and os.stat(path).st_nlink == 1 and sorted(os.listdir(lib)) == [path.name]
        return
    if case == "killed at the rename":   # the kept link exists by then. The "killed" case stops in keep_original() first.
        real = os.replace
        monkeypatch.setattr(hook.os, "replace", lambda a, b: (_ for _ in ()).throw(SystemExit(143)) if b == str(path) else real(a, b))
        with pytest.raises(SystemExit):
            hook.repack(str(path), j, st, True, hp)
        assert path.read_bytes() == before and os.stat(path).st_nlink == 1 and sorted(os.listdir(lib)) == [path.name]
        assert not [n for _, _, ns in os.walk(root) for n in ns if n == path.name]
        return
    if case == "trim fails":
        hp = dict(hp, trim=[99])
        monkeypatch.setattr(hook, "trim_inputs", lambda *a: (_ for _ in ()).throw(ValueError("a SubRip block with no timing line")))
    code, result, info = hook.repack(str(path), j, st, True, hp)
    assert code == {"no place": "header_repair_skipped", "kept": "header_repaired", "pruned": "header_repaired"}.get(case, "header_repair_failed"), result
    if case == "trim fails":
        assert result == "header repair failed: a SubRip block with no timing line" and "warnings" in info
        assert path.read_bytes() == before and sorted(os.listdir(lib)) == [path.name]
        return
    if case == "no place":
        assert result == f"header repair skipped, the original cannot be kept: {root} is on another file system: " + hp["issue"][0]
        assert path.read_bytes() == before and "warnings" not in info
        return
    if case == "link fails":
        assert result == "header repair failed: the original could not be kept, so it stays: [Errno 5] Input/output error"
        assert path.read_bytes() == before and sorted(os.listdir(lib)) == [path.name]
        return
    kept = info["kept"]
    assert result == "header repaired" and kept.startswith(str(root) + "/") and kept.endswith("/movies/Movie (2000)/Movie (2000).mkv")
    assert open(kept, "rb").read() == before and os.stat(kept).st_ino == st.st_ino and path.read_bytes() != before
    assert not old.exists()   # a stamp from 2000 is older than a week
    if case == "pruned":   # the nightly audit prunes the roots of the app's root folders
        monkeypatch.setattr(hook, "time", types.SimpleNamespace(**{k: getattr(time, k) for k in ("gmtime", "strptime", "strftime", "monotonic", "sleep")},
                                                                time=lambda: time.time() + 8 * 86400))
        assert hook.prune_originals(str(root)) == [os.path.relpath(kept, root).split("/")[0]] and not os.path.exists(kept)


def test_the_prune_goes_on_past_a_failed_folder(tmp_path, monkeypatch, settings):
    """The audits of two hosts can remove the same folder at once. One failure keeps only that folder."""
    settings(keep_days=7)
    for n in ("20000101T000000Z", "20000102T000000Z", "not a stamp"):
        (tmp_path / n).mkdir()
    real = shutil.rmtree
    def rmtree(p, *a, **k):
        if p.endswith("20000101T000000Z"):
            raise OSError(2, "No such file or directory")
        real(p, *a, **k)
    monkeypatch.setattr(hook.shutil, "rmtree", rmtree)
    assert hook.prune_originals(str(tmp_path)) == ["20000102T000000Z"]
    assert sorted(os.listdir(tmp_path)) == ["20000101T000000Z", "not a stamp"]


def test_the_mount_holds_the_originals_above_every_root_folder(tmp_path, monkeypatch):
    mount = "/data"
    monkeypatch.setattr(hook.os.path, "ismount", lambda p: p in (mount, "/"))
    assert hook.originals_root(f"{mount}/movies/Film G/Film G.mkv") == f"{mount}/.arr-media-guard-originals"
    assert hook.replaced_root(f"{mount}/movies/Film G/Film G.mkv") == f"{mount}/.arr-media-guard-recycle"


def test_a_remux_that_lost_frames_is_refused(mkvs, tmp_path, monkeypatch, settings):
    """mkvmerge can drop a damaged cluster with no warning, so a zero hole can cost frames. The
    new video track must hold its end over its default duration in frames, less FRAME_SLACK. A track with no default
    duration passes only when every window of the check decoded clean."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = str(mkvs / "good.mkv"); j = hook.mkvmerge(path)
    hp = {"expect": 120.023, "video": 120.0, "issue": ["x"], "trim": []}
    short = copy.deepcopy(j); short["tracks"][0]["properties"]["tag_number_of_frames"] = str(2880 - 32)
    assert hook.header_fault(j, path, short, os.path.getsize(path), hp, {}) == \
        "the new file holds 2848 video frames, but the video end at 120.0 s needs 2880"
    slack = copy.deepcopy(j); slack["tracks"][0]["properties"]["tag_number_of_frames"] = str(2880 - hook.arr_decide.FRAME_SLACK)
    assert hook.header_fault(j, path, slack, os.path.getsize(path), hp, {}) is None
    vfr = copy.deepcopy(j); del vfr["tracks"][0]["properties"]["default_duration"]
    assert hook.header_fault(j, path, vfr, os.path.getsize(path), hp, {}).startswith("the video track has no default duration")
    assert hook.header_fault(j, path, vfr, os.path.getsize(path), dict(hp, windows_clean=True), {}) is None
    renamed = copy.deepcopy(j); renamed["tracks"][1]["properties"]["forced_track"] = True
    assert hook.header_fault(j, path, renamed, os.path.getsize(path), hp, {}) == "track 1 changed its forced_track"


def test_a_removal_that_does_not_match_the_plan_keeps_the_original(mkvs, tmp_path, monkeypatch, settings):
    """The remux counts the lines again with trim_srt(). A track on the other side of the rule than the plan stops it, and
    the fault check takes only the planned track as missing."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "othercut.mkv"; shutil.copy(mkvs / "othercut.mkv", path); before = path.read_bytes()
    j = hook.mkvmerge(str(path)); h = hook.video_check(str(path))[2]["header"]
    monkeypatch.setattr(hook.arr_decide, "REMOVE_SHARE", 0.5)   # 8 of 24 is now a trim, not the planned removal
    _, result, _ = hook.repack(str(path), j, os.stat(path), True, h)
    assert result == ("header repair failed: subtitle track 3 has 8 of 24 lines starting after the end, so the plan to remove it "
                      "no longer holds"), result
    assert path.read_bytes() == before and not os.path.exists(os.path.dirname(hook.repack_tmp(str(path))))
    other = copy.deepcopy(j); del other["tracks"][2]   # the English track went instead of the Italian one
    assert hook.header_fault(j, str(path), other, os.path.getsize(path), dict(h, remove=[3]), {}).startswith("the tracks changed")
    assert hook.header_fault(j, str(path), j, os.path.getsize(path), dict(h, remove=[3]), {}).startswith("the tracks changed")


def test_a_backfill_leaves_the_rescan_of_a_repair_to_the_end_of_its_run(env, mkvs, tmp_path, monkeypatch):
    """A backfill sends one rescan per item after its last file, so the repair of one file sends none."""
    monkeypatch.setattr(hook, "mkvmerge", REAL_MKVMERGE)
    monkeypatch.setattr(hook, "window", REAL_WINDOW)
    monkeypatch.setattr(hook.subprocess, "run", REAL_RUN)
    path = tmp_path / "media" / "Film F" / "Film F.mkv"
    path.parent.mkdir(parents=True); shutil.copy(mkvs / "othercut.mkv", path)
    rec = hook.process(hook.Ctx("radarr", str(path), "Film F", "English", 2, mode="backfill", apply=True, post=False, ids={"app_id": 7}))
    assert rec["header_repair"]["code"] == "subtitle_removed" and rec["header_repair"]["rescan"] == "after the run", rec["header_repair"]
    assert env["writes"] == []


def test_a_removed_default_subtitle_leads_to_a_new_decision_on_the_new_file(env, mkvs, tmp_path, monkeypatch):
    """The Italian track is the default subtitle, and the removal takes it out. The header step runs before the decision,
    and process() probes the new file after the remux, so the flags are decided on the file without that track."""
    monkeypatch.setattr(hook, "mkvmerge", REAL_MKVMERGE)
    monkeypatch.setattr(hook, "window", REAL_WINDOW)
    monkeypatch.setattr(hook.subprocess, "run", REAL_RUN)
    path = tmp_path / "media" / "Film F" / "Film F.mkv"
    path.parent.mkdir(parents=True); shutil.copy(mkvs / "othercut.mkv", path)
    before = [(t["properties"]["language"], t["properties"]["default_track"]) for t in hook.mkvmerge(str(path))["tracks"] if t["type"] == "subtitles"]
    rec = hook.process(hook.Ctx("radarr", str(path), "Film F", "English", 2, mode="backfill", apply=True, post=False))
    assert before == [("eng", False), ("ita", True)]
    assert rec["header_repair"]["code"] == "subtitle_removed" and rec["reasons"][0] == "subtitle_removed", rec.get("header_repair")
    assert [t["tag"] for t in rec["tracks"] if t["i"].startswith("s")] == ["eng"]   # the decision read the new file
    assert [r["result"] for r in log_lines(env)][0] == "subtitles removed"


@pytest.mark.parametrize("kind", ["header", "mp4"])
def test_a_refused_chown_keeps_the_repair_going(mkvs, media, tmp_path, monkeypatch, settings, kind):
    """A NAS share may refuse chown with [Errno 1]. The new file stays root's, the chmod copies the mode, and info
    says the owner changed. Radarr and Sonarr may run as root there."""
    settings(log=str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(hook.os, "chown", lambda p, uid, gid: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")))
    path = tmp_path / "Movie (2000).mkv"
    shutil.copy(mkvs / "long.mkv" if kind == "header" else media / "good.mp4", path); os.chmod(path, 0o640)
    st, j = os.stat(path), hook.mkvmerge(str(path))
    if kind == "header":
        _, result, info = hook.repack(str(path), j, st, True, hook.header_of(str(path), j))
    else:   # an MP4 under a .mkv name keeps its name, so the conversion needs no re-link
        _, result, info, _ = hook.convert("radarr", str(path), j, st, True, {})
    now = os.stat(path)
    assert result == ("header repaired" if kind == "header" else "repacked") and now.st_mode & 0o777 == 0o640, (result, info)
    assert info["owner"] == {"from": f"{st.st_uid}:{st.st_gid}", "to": f"{now.st_uid}:{now.st_gid}",
                             "why": "chown refused: [Errno 1] Operation not permitted"}


def test_a_header_repair_refuses_an_original_the_app_changed(mkvs, tmp_path, monkeypatch, settings):
    """The app wrote the original while the remux ran, and only its mtime shows it. The rename would overwrite the app's
    file, so the temp file goes, the original stays, and nothing is kept."""
    settings(log=str(tmp_path / "log.jsonl"))
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    lib = tmp_path / "Movie (2000)"; lib.mkdir()
    path = lib / "Movie (2000).mkv"; shutil.copy(mkvs / "long.mkv", path)
    st, j, real = os.stat(path), hook.mkvmerge(str(path)), hook.mkvmerge
    hp = hook.header_of(str(path), j)
    monkeypatch.setattr(hook, "mkvmerge", lambda p: (os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9)), real(p))[1])
    _, result, info = hook.repack(str(path), j, st, True, hp)
    assert result == "header repair failed, the original changed: the app replaced or renamed the original during the header repair", result
    assert os.stat(path).st_ino == st.st_ino and "kept" not in info and not (tmp_path / ".kept").exists()
    assert os.listdir(lib) == [path.name]


@pytest.mark.parametrize("runtime", [5, 0])
def test_a_cut_file_keeps_its_good_subtitles(env, mkvs, tmp_path, monkeypatch, runtime):
    """A file cut at 60 percent ends early, and its good SubRip track runs to the full length. It passes the
    removal rule, so without a gate the track would go and no alert would post. A file whose video and audio end under
    CUT_END of the listed runtime, with a late track ending at or under REMOVE_END of it, may be cut. With no runtime the
    gate blocks too. The file stays as it is, with one amber alert."""
    monkeypatch.setattr(hook, "mkvmerge", REAL_MKVMERGE)
    monkeypatch.setattr(hook, "window", REAL_WINDOW)
    monkeypatch.setattr(hook.subprocess, "run", REAL_RUN)
    path = tmp_path / "media" / "Cut (2020)" / "Cut (2020).mkv"
    path.parent.mkdir(parents=True); shutil.copy(mkvs / "cut.mkv", path); before = path.read_bytes()
    h = hook.header_of(str(path))
    assert (h["remove"], h["sublines"], h["streams"]) == ([2], {2: [50, 19]}, 180.024), h   # the rule alone would remove it
    rec = hook.process(hook.Ctx("radarr", str(path), "Cut (2020)", "English", runtime, mode="backfill", apply=True, post=False))
    why = ("the video and audio stop at 3:00, but the listed runtime is 5 minutes, and the subtitles run to 4:57" if runtime else
           "the subtitles run far past the video and audio, and no runtime is listed to tell whether the file is cut short")
    assert rec["header_repair"]["code"] == "subtitle_file_may_be_cut" and rec["header_repair"]["result"].endswith(why), rec["header_repair"]
    assert {"kind": "cut", "why": why} in rec["findings"] and "cut" in rec["alert_kinds"]
    assert [t["tag"] for t in rec["tracks"] if t["i"].startswith("s")] == ["eng"]
    edited = hook.mkvmerge(str(path))   # the flag edit may run, the subtitle track and the duration stay
    assert [t["type"] for t in edited["tracks"]] == ["video", "audio", "subtitles"] and hook.arr_decide.duration(edited) > 290
    assert os.path.getsize(path) == len(before)


@pytest.fixture(scope="module")
def cuts(tmp_path_factory):
    """Two cut episodes in small: 10 minutes of video for a listed 12 (0.83). In the first, the only SubRip track
    runs to 0.98 of the listed runtime with 90 of 604 lines late. In the second, the track runs to 0.93 with 23 of 450
    lines late."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d = tmp_path_factory.mktemp("cuts")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=600", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=600", "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-c:a", "aac", str(d / "src.mkv")],
                   check=True)
    srt = lambda events: "\n\n".join(f"{k + 1}\n{stamp(a)} --> {stamp(b)}\n{text}" for k, (a, b, text) in enumerate(events)) + "\n"
    (d / "remove.srt").write_text(srt([(k * 706 / 604, k * 706 / 604 + 1, f"Line {k}") for k in range(604)]))
    (d / "trim.srt").write_text(srt([(k * 1.4, k * 1.4 + 1, f"Line {k}") for k in range(427)]
                                     + [(601 + k * 2.95, 602 + k * 2.95, f"Late {k}") for k in range(23)]))
    for name in ("remove", "trim"):
        subprocess.run(["mkvmerge", "-q", "-o", str(d / f"{name}.mkv"), str(d / "src.mkv"), "--language", "0:eng", str(d / f"{name}.srt")],
                       check=True)
    os.remove(d / "src.mkv")
    return d


@pytest.mark.parametrize("name, late", [("remove", {2: [604, 90]}), ("trim", {2: [450, 23]})])
def test_a_cut_episode_is_never_trimmed_or_stripped(env, cuts, tmp_path, monkeypatch, name, late):
    """A share of the listed runtime alone passes both files at 0.83, so the first would lose its only subtitle
    track and the second would get a trim. Either would set the header to the cut end and stop the duration alert. A
    late track that ends near the listed runtime marks a cut file. It stays as it is, with the amber "File may be cut"."""
    monkeypatch.setattr(hook, "mkvmerge", REAL_MKVMERGE)
    monkeypatch.setattr(hook, "window", REAL_WINDOW)
    monkeypatch.setattr(hook.subprocess, "run", REAL_RUN)
    path = tmp_path / "media" / "Show" / f"{name}.mkv"
    path.parent.mkdir(parents=True); shutil.copy(cuts / f"{name}.mkv", path)
    h = hook.header_of(str(path))
    plan = {"remove": ([], [2]), "trim": ([2], [])}[name]   # what the probe plans before the runtime is known
    assert ((h["trim"], h["remove"]), h["sublines"]) == (plan, late), h
    rec = hook.process(hook.Ctx("sonarr", str(path), "Show S01E01", "English", 12, mode="backfill", apply=True, post=False))
    end = {"remove": "11:45", "trim": "11:06"}[name]
    assert rec["header_repair"]["result"] == ("not repaired, the file may be cut: the video and audio stop at 10:00, but the listed runtime is "
                                              f"12 minutes, and the subtitles run to {end}"), rec["header_repair"]
    assert rec["header_repair"]["code"] == "subtitle_file_may_be_cut" and rec["alert_kinds"][-1] == "cut"
    after = hook.mkvmerge(str(path))
    assert [t["type"] for t in after["tracks"]] == ["video", "audio", "subtitles"] and hook.arr_decide.duration(after) > 660
    assert not os.path.exists(os.path.dirname(hook.repack_tmp(str(path))))


# --- restore after a bad upgrade ------------------------------------------------------------------

TONE = "[x] [info] n_samples: 1922128\n[x] [info] max_volume: -4.0 dB\n"


def recycled(src, folder):
    """RecycleBinProvider.DeleteFile() of Sonarr 4 and Radarr 6: the file goes to <bin>/<subfolder>/<name>, and a name the
    bin holds already gets _2, _3 before the extension. Returns the new path."""
    stem, ext = os.path.splitext(os.path.basename(src))
    dest, i = os.path.join(folder, stem + ext), 1
    while os.path.exists(dest):
        i += 1
        dest = os.path.join(folder, f"{stem}_{i}{ext}")
    os.makedirs(folder, exist_ok=True)
    os.rename(src, dest)
    return dest


def silent(env, monkeypatch, *paths):
    """Digital silence in the audio of paths, a tone in every other file."""
    real = hook.subprocess.run
    def run(argv, **k):
        if argv[0] != "ffmpeg":
            return real(argv, **k)
        env["ffmpeg"].append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": SILENCE if argv[argv.index("-i") + 1] in paths else TONE})()
    monkeypatch.setattr(hook.subprocess, "run", run)


def upgrade(env, monkeypatch, old_name, recycle_name=None, links=True, grab=True):
    """A Radarr upgrade: the old file was <movie folder>/<old_name>, and the app moved it to its recycle bin as
    recycle_name. Sets the variables the app passes, and plays the app: a DELETE moves the new file into the bin, removes
    the movie folder when it is empty (deleteEmptyFolders) and unmonitors the movie (autoUnmonitorPreviouslyDownloadedMovies).
    The rescan links the old file when links, and the editor PUT monitors. grab False is a manual import, with no grab
    record. Returns (old path, recycle bin path)."""
    folder = os.path.dirname(env["path"])
    bin_ = os.path.join(os.path.dirname(folder), ".recycle", "radarr", os.path.basename(folder))   # /data/.recycle/radarr
    old, rb = os.path.join(folder, old_name), os.path.join(bin_, recycle_name or old_name)
    os.makedirs(bin_, exist_ok=True)
    open(rb, "wb").write(b"old" * 300)
    if grab:
        grabbed(env, monkeypatch)
    else:
        monkeypatch.setenv("radarr_moviefile_id", "11")
    for k, v in (("radarr_isupgrade", "True"), ("radarr_deletedpaths", old), ("radarr_deletedrecyclebinpaths", rb)):
        monkeypatch.setenv(k, v)
    movie = env["movies"]["movie/7"]
    env["movies"]["command/1"] = {"id": 1, "status": "completed" if links else "started"}
    def write(app, p, method, body=None):
        env["writes"].append((method, p, body))
        if method == "DELETE":
            env["logged_at_delete"] = [r["result"] for r in log_lines(env)] if os.path.exists(hook.CFG.log) else []
            env.get("at_delete", lambda: None)()   # what else happens to the bin at that moment
            recycled(env["path"], bin_)
            with contextlib.suppress(OSError):   # MediaFileDeletionService.Handle(), inside the DELETE call
                os.rmdir(folder)
            movie.pop("movieFile", None); movie["monitored"] = False
        elif p == "command":
            if links and os.path.exists(old):
                movie["movieFile"] = {"id": 12, "path": old}
            return {"id": 1, "status": "queued"}
        elif p == "movie/editor":
            movie["monitored"] = body["monitored"]
    monkeypatch.setattr(hook, "arr_write", write)
    return old, rb


def action(env):
    """The one finding with an action on the decision line, and its action. Its alert went out, unless the hook re-grabbed
    or put the old file back where the app picked it up, which goes to the decision log only, see report.fixed()."""
    (f,) = [f for f in decided(env)["findings"] if f.get("action")]
    fixed = hook.fixed(f["action"])
    assert len([b for m, u, b in env["http"] if m == "POST"]) == (not fixed) and decided(env)["alert_result"] == ["log only" if fixed else "sent"]
    return f, f["action"]


def test_the_hook_keeps_the_old_files_of_an_upgrade_in_the_job(env, monkeypatch):
    """Sonarr 4 and Radarr 6 pass <app>_deletedpaths and <app>_deletedrecyclebinpaths, pipe-separated, on an upgrade. .NET's
    StringDictionary lowers the names. A path is empty when the app has no recycle bin or the old file was not on disk."""
    monkeypatch.setattr(hook.os, "fork", lambda: 4242)
    for k, v in (("radarr_isupgrade", "True"), ("radarr_deletedpaths", "/m/A (2000)/A (2000) HDTV-720p.mkv|/m/A (2000)/A.cd2.mkv"),
                 ("radarr_deletedrecyclebinpaths", "/bin/A (2000)/A (2000) HDTV-720p.mkv|")):
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit):
        hook.main([])
    (name,) = queue(env)
    job = hook.job_of(name)
    assert hook.old_files(job) == ([("/m/A (2000)/A (2000) HDTV-720p.mkv", "/bin/A (2000)/A (2000) HDTV-720p.mkv"), ("/m/A (2000)/A.cd2.mkv", "")], None)
    assert hook.old_files(dict(job, recycled="/bin/x.mkv")) == ([], "the app's lists of old paths and recycle bin paths do not pair up")
    assert hook.old_files(dict(job, deleted=None)) == ([], None)   # an import that replaced nothing


def test_a_broken_upgrade_puts_the_old_file_back(env, monkeypatch):
    """An upgrade replaced an HDTV-720p mp4 with a silent WEBDL-1080p mkv, another name and another extension. The old file
    checks clean in the recycle bin, so it goes back to its path. The app drops the broken upgrade into its bin, rescans,
    links the old file, and the grab is marked failed last. Plex re-analyzes the item at the old path."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mp4")
    silent(env, monkeypatch, env["path"])
    sent = refreshes(env, monkeypatch)
    hook.main([])
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "command", {"name": "RescanMovie", "movieId": 7}), ("POST", "history/failed/2101", None)]
    assert open(old, "rb").read() == b"old" * 300 and not os.path.exists(rb)
    (line,) = [r for r in log_lines(env) if r["result"] == "restoring"]   # written before the delete, see upgrade()
    assert line["delete"] == [11] and line["moves"] == [{"file_id": 11, "old": old, "recycle": rb, "extras": []}]
    assert env["logged_at_delete"] == ["restoring"]
    assert os.path.exists(os.path.join(os.path.dirname(rb), os.path.basename(env["path"])))   # the broken upgrade, in the bin
    rec = decided(env)
    assert rec["outcome"] == "broken_audio" and rec["reasons"][0] == "old_file_restored"
    (r,) = rec["restore"]
    assert (r["file_id"], r["result"], r["old"], r["recycle"], r["rescan"], r["linked"]) == (11, "restored", old, rb, "completed", True)
    assert r["check"]["audio"]["certain"] is None and r["check"]["video"]["certain"] is None and len(r["check"]["video"]["windows"]["list"]) == 3
    assert {a[a.index("-i") + 1] for a in env["ffmpeg"]} == {env["path"], rb}   # the old file got the same audio check
    f, act = action(env)
    assert (f["kind"], f["certain"], hook.action_code(act)) == ("audio", "the audio is silent at all 3 places checked", "restored")
    assert act == {"code": "regrabbed", "name": "Radarr", "kind": "audio", "n": 1, "came": ["Film A (1979) HDTV-720p.mp4"], "linked": True,
                   "own_copy": False, "others": 0, "stayed": None}
    # another name: Plex scans the one folder, and never analyzes the item for it
    assert analyzes(env) == [] and sent == [("GET", "/library/sections/12/refresh", {"path": [os.path.dirname(old)], "X-Plex-Token": ["plex-t0ken-1234"]})]
    assert [r["plex_reason"] for r in log_lines(env) if r["result"] == "plex"] == ["plex_scan_sent"]


def test_a_same_name_upgrade_restores_the_copy_its_own_upgrade_recycled(env, monkeypatch):
    """Two upgrades of one item within a day, with the same file name. The first upgrade's old file is <name>.mkv
    and this upgrade's is <name>_2.mkv. The app names the exact copy, so the older one stays. The broken upgrade
    frees the path first and lands in the bin as <name>_3.mkv."""
    name = os.path.basename(env["path"])
    old, rb = upgrade(env, monkeypatch, name, name.replace(".mkv", "_2.mkv"))
    first = os.path.join(os.path.dirname(rb), name)
    open(first, "wb").write(b"first")
    open(env["path"], "wb").write(b"new" * 300)
    silent(env, monkeypatch, env["path"])
    sent = refreshes(env, monkeypatch)
    hook.main([])
    assert old == env["path"] and open(old, "rb").read() == b"old" * 300
    assert analyzes(env) == ["/library/metadata/7101/analyze"] and sent == []   # the same name: Plex re-reads the part it knows
    assert open(first, "rb").read() == b"first" and not os.path.exists(rb)
    assert open(os.path.join(os.path.dirname(rb), name.replace(".mkv", "_3.mkv")), "rb").read() == b"new" * 300
    assert decided(env)["restore"][0]["linked"] is True and [m for m, p, b in env["writes"]] == ["DELETE", "PUT", "POST", "POST"]
    assert "trusted" not in decided(env)   # the metadata checks never read the old file under the new file's probe


@pytest.mark.parametrize("case, why", [
    ("pruned", "the recycle bin no longer holds it"),
    ("taken", "another file holds its path now"),
    ("audio", "its audio is broken too: the audio is silent at all 3 places checked"),
    ("video", "its video is corrupt too: the video is broken at 3 of 3 places checked"),
    ("volume", "the recycle bin is on another volume, and a restore never copies"),
    ("no bin", "the app kept no copy in its recycle bin"),
    ("no decoder", "3 of 3 of its audio samples did not run"),
    ("window stopped", "its video check did not run to its end")])
def test_an_old_file_that_may_not_come_back_leaves_the_plain_regrab(env, monkeypatch, case, why):
    """The recycle bin pruned it, a file took the old path, the old file is broken too, the bin sits on another
    volume, or the app has no bin. The re-grab runs as before the restore existed, and the one alert names the
    reason. Nothing is written in the library."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv")
    silent(env, monkeypatch, env["path"], *([rb] if case == "audio" else []))
    if case == "pruned":
        os.remove(rb)
    elif case == "taken":
        open(old, "wb").write(b"other")
    elif case == "video":
        real = hook.window
        monkeypatch.setattr(hook, "window", lambda p, s, secs: dict(real(p, s, secs), **(BAD_WINDOW if p == rb else {})))
    elif case == "volume":
        monkeypatch.setattr(hook, "volume", lambda p: 64 if "/.recycle/" in p else 63)
    elif case == "no bin":
        monkeypatch.setenv("radarr_deletedrecyclebinpaths", "")
    elif case == "no decoder":   # the old file's CodecID has no decoder, and each sample exits 234
        tone = hook.subprocess.run
        monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: type("R", (), {"returncode": 234, "stdout": "", "stderr": "[fatal] Decoder "
                            "not found"})() if argv[0] == "ffmpeg" and argv[argv.index("-i") + 1] == rb else tone(argv, **k))
    elif case == "window stopped":   # the read cap stopped it: the file may have no index, and a stopped window is never clean
        real = hook.window
        monkeypatch.setattr(hook, "window", lambda p, s, secs: dict(real(p, s, secs), **({"stopped": "read over 512 MiB"} if p == rb else {})))
    hook.main([])
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "history/failed/2101", None)]
    rec = decided(env)
    (r,) = rec["restore"]
    assert rec["reasons"][0] == "old_file_not_restored" and r["result"].startswith(f"not restored: {why}"), r
    f, act = action(env)
    assert (act["code"], act["n"], act["came"]) == ("regrabbed", 1, []) and act["stayed"].startswith(why), act
    assert os.path.exists(rb) == (case != "pruned") and (open(old, "rb").read() == b"other" if case == "taken" else not os.path.exists(old))


def test_a_restore_the_app_does_not_link_in_time_still_fails_the_grab(env, monkeypatch):
    """The rescan did not end within RESTORE_WAIT. The old file is back on disk, the read-back finds no file record, the
    alert posts, because the app did not pick the old file up, and the grab is still marked failed so the broken release is
    blocklisted."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", links=False)
    silent(env, monkeypatch, env["path"])
    hook.main([])
    assert os.path.exists(old) and env["writes"][-1] == ("POST", "history/failed/2101", None)
    (r,) = decided(env)["restore"]
    assert (r["rescan"], r["linked"], r["read_back"]) == ("waited", False, "Radarr lists ['no file'], monitored [True]")
    assert env["sleeps"][:60] == [2] * 60   # polled the command every 2 seconds for 120
    assert (action(env)[1]["came"], action(env)[1]["linked"]) == (["Film A (1979) HDTV-720p.mkv"], False)


@pytest.mark.parametrize("armed", [False, True])
def test_wrong_content_restores_only_when_its_regrab_is_armed(env, monkeypatch, settings, armed):
    """A wrong language alone scores one point and only alerts. With the release name naming that language it is wrong
    content, which re-grabs only with content in REGRAB. The restore follows that list."""
    wrong_film(env, monkeypatch)
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv")
    settings(regrab={"audio", "video", "content"} if armed else {"audio", "video"})
    hook.main([])
    rec = decided(env)
    assert os.path.exists(old) == armed and os.path.exists(rb) != armed
    assert rec["outcome"] == ("wrong_content" if armed else "would_regrab") and bool(rec.get("restore")) == armed
    (f,) = [f for f in rec["findings"] if f.get("action")]
    assert (f["kind"], hook.action_code(f["action"])) == ("content", "restored" if armed else "would_regrab")


@pytest.mark.parametrize("parse, why", [
    ([104], None),
    ([104, 105], "the old file also holds episodes [105], which the broken file does not"),
    ([], "Sonarr cannot tell which episodes the old file holds"),
    ([9104], "Sonarr's parse maps the old file to other episodes [9104], as scene or absolute numbering does")])
def test_a_season_pack_restores_what_it_can_and_regrabs_the_rest(env, monkeypatch, tmp_path, parse, why):
    """A pack whose episodes 1, 4 and 7 are silent. 1 and 4 were upgrades with a clean old file in Sonarr's bin, and
    episode 4's own job still waits in the queue with its old file. 7 replaced nothing. One unit: two old files come
    back, 7 is only deleted, one rescan of the series, one failed mark. Episode 4's old file must hold no
    episode the broken file does not. An old s01e04-e05 file stays in the bin, because Sonarr checks every episode a
    file maps to, and a file may hold more than one. So does an old file Sonarr cannot parse."""
    paths = pack(env, monkeypatch, tmp_path, broken=(1, 4, 7))
    folder, bin_ = os.path.dirname(paths[201]), str(tmp_path / "media" / ".recycle" / "sonarr" / "Show" / "Season 1")
    old = {n: os.path.join(folder, f"Show - s01e{n:02d} - HDTV-720p.mkv") for n in (1, 4)}
    rb = {n: os.path.join(bin_, os.path.basename(old[n])) for n in (1, 4)}
    os.makedirs(bin_)
    for n in (1, 4):
        open(rb[n], "wb").write(b"old")
    hook.store.write("INSERT INTO jobs (name, at, job) VALUES (?, 0, ?)", "9" * 19 + "-1.json", json.dumps(   # younger than 201's job
        {"app": "sonarr", "event": "Download", "time": env["clock"][0], "path": paths[204], "owner": "5", "file_id": "204",
         "episode_ids": "104", "download_id": "pack1", "release": None, "deleted": old[4], "recycled": rb[4]}))
    linked, watched, base = {}, {}, hook.arr   # the app's side after the rescan: episode id -> path, and the monitored flags
    parsed = {old[1]: [101], old[4]: parse}   # Sonarr's parse API: the old path's episodes
    def arr(app, p):
        if p.startswith("parse?"):
            q = parse_qs(p.split("?", 1)[1])
            assert q["title"] == [os.path.basename(q["path"][0])]   # Sonarr returns nothing without a title
            return {"episodes": [{"id": i} for i in parsed.get(q["path"][0], [])]}
        if p == "command/1":
            return {"id": 1, "status": "completed"}
        if p.startswith("episode?episodeIds=") and linked:
            return [{"id": int(i), "episodeFileId": 200 + int(i) if int(i) in linked else 0, "monitored": watched.get(int(i), False),
                     "seriesId": 5} for i in parse_qs(p.split("?", 1)[1])["episodeIds"]]
        if p.startswith("episodefile/3"):
            return {"path": linked[int(p.split("/")[1]) - 200]}
        return base(app, p)
    def write(app, p, method, body=None):
        env["writes"].append((method, p, body))
        if method == "DELETE":
            recycled(paths[int(p.split("/")[1])], bin_)
        elif p == "command":
            linked.update({100 + n: old[n] for n in (1, 4) if os.path.exists(old[n])})
            return {"id": 1, "status": "queued"}
        elif p == "episode/monitor":
            watched.update(dict.fromkeys(body["episodeIds"], True))
    monkeypatch.setattr(hook, "arr", arr)
    monkeypatch.setattr(hook, "arr_write", write)
    monkeypatch.setenv("sonarr_deletedpaths", old[1])
    monkeypatch.setenv("sonarr_deletedrecyclebinpaths", rb[1])
    import_event(monkeypatch, paths, 201)
    assert env["writes"] == [("DELETE", "episodefile/201", None), ("DELETE", "episodefile/204", None), ("DELETE", "episodefile/207", None),
                             ("PUT", "episode/monitor", {"episodeIds": [101, 104, 107], "monitored": True}),
                             ("POST", "command", {"name": "RescanSeries", "seriesId": 5}), ("POST", "history/failed/900", None)]
    assert os.path.exists(old[4]) == (not why) and os.path.exists(rb[4]) == bool(why)
    assert os.path.exists(old[1]) and not os.path.exists(rb[1]) and not os.path.exists(paths[207])
    rec = [r for r in log_lines(env) if r.get("path") == paths[201] and r.get("outcome")][-1]
    assert [(r["file_id"], r["result"], r.get("linked")) for r in rec["restore"]] == [(201, "restored", True)] + (
        [(204, "restored", True)] if not why else [(204, f"not restored: {why}", None)])
    if why:
        return
    assert rec["findings"][0]["action"] == {"code": "regrabbed", "name": "Sonarr", "kind": "audio", "n": 3, "came": ["Show - s01e01 - HDTV-720p.mkv"],
                                            "linked": True, "own_copy": False, "others": 1, "stayed": None}
    assert [(r["outcome"], r.get("fault")) for r in log_lines(env) if r.get("path") == paths[204] and r.get("outcome")][-1] == \
        ("deleted_with_download", "audio")   # its own job runs after the unit
    assert regrabs_counted() == 1


def test_the_extras_come_back_after_the_video_and_a_taken_path_stays(env, monkeypatch):
    """The bin holds this upgrade's video as <name>_2.mkv. Its .nfo clashed
    too and is <name>_2.nfo, its .en.srt did not. The older copies of an upgrade two days ago stay, a .fr.srt with no newer
    copy too, because it was not recycled with this video. A thumb whose path is
    taken stays in the bin after a wait. A file of another name ("... Proper.nfo") is no extra of it."""
    stem = "Film A (1979) HDTV-720p"
    old, rb = upgrade(env, monkeypatch, stem + ".mkv", stem + "_2.mkv")
    silent(env, monkeypatch, env["path"])
    bin_, lib, now = os.path.dirname(rb), os.path.dirname(old), time.time()
    files = {stem + ".mkv": -2 * 86400, stem + ".nfo": -2 * 86400, stem + "_2.nfo": 1, stem + ".en.srt": 1, stem + "-thumb.jpg": 2,
             stem + " Proper.nfo": 1, stem + ".fr.srt": -2 * 86400, stem + "_2.mkv": 0}
    for n, age in files.items():
        if n != stem + "_2.mkv":
            open(os.path.join(bin_, n), "w").write(n)
        os.utime(os.path.join(bin_, n), (now + age, now + age))
    open(os.path.join(lib, stem + "-thumb.jpg"), "w").write("taken")
    hook.main([])
    (r,) = decided(env)["restore"]
    assert r["result"] == "restored" and open(old, "rb").read() == b"old" * 300
    assert [(os.path.basename(x["file"]), os.path.basename(x["recycle"]), x["result"]) for x in r["extras"]] == [
        (stem + "-thumb.jpg", stem + "-thumb.jpg", "stays in the bin: another file holds its path"),
        (stem + ".en.srt", stem + ".en.srt", "restored"), (stem + ".nfo", stem + "_2.nfo", "restored")]
    assert open(os.path.join(lib, stem + ".nfo")).read() == stem + "_2.nfo" and open(os.path.join(lib, stem + "-thumb.jpg")).read() == "taken"
    assert sorted(os.listdir(bin_)) == sorted([stem + ".mkv", stem + ".nfo", stem + "-thumb.jpg", stem + " Proper.nfo", stem + ".fr.srt",
                                               os.path.basename(env["path"])])   # the older copies, the kept thumb, the broken upgrade
    assert env["sleeps"].count(1) == hook.EXTRA_WAIT   # the taken thumb waited for Radarr's delete of the broken file's own extras
    assert [m for m, p, b in env["writes"]] == ["DELETE", "PUT", "POST", "POST"]   # the re-monitor and the rescan come after the extras


def test_a_file_that_takes_the_old_path_during_the_delete_is_never_overwritten(env, monkeypatch):
    """The old path is free at the plan, and a file takes it during the app's delete. restore() checks the path again
    right before the rename, so that file stays and the old file stays in the bin."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv")
    silent(env, monkeypatch, env["path"])
    env["at_delete"] = lambda: open(old, "wb").write(b"other")
    hook.main([])
    (r,) = decided(env)["restore"]
    assert r["result"] == "not restored: another file holds its path now" and open(old, "rb").read() == b"other" and os.path.exists(rb)


def test_an_extra_that_changed_in_the_bin_since_the_plan_stays_there(env, monkeypatch):
    """Another file takes the extra's bin name between the plan and the restore. Its bin signature differs from the
    plan's, so it stays in the bin, and the video still comes back."""
    stem = "Film A (1979) HDTV-720p"
    old, rb = upgrade(env, monkeypatch, stem + ".mkv")
    silent(env, monkeypatch, env["path"])
    srt, target = os.path.join(os.path.dirname(rb), stem + ".en.srt"), os.path.join(os.path.dirname(old), stem + ".en.srt")
    open(srt, "w").write("old")
    env["at_delete"] = lambda: open(srt, "w").write("another file")
    hook.main([])
    (r,) = decided(env)["restore"]
    assert r["result"] == "restored" and r["extras"] == [{"file": target, "recycle": srt, "result": "stays in the bin: it changed there since the plan"}]
    assert open(srt).read() == "another file" and not os.path.exists(target)


@pytest.mark.parametrize("monitored", [True, False])
def test_a_broken_manual_import_gets_the_old_file_back_without_a_search(env, monkeypatch, monitored):
    """A manual import has no grab record. When it replaced a file and is broken, the app deletes it into its bin and the
    checked old file comes back, with one alert. No grab is marked failed, so the app does not search. The movie ends
    monitored only when it was before, because the app's delete unmonitors it."""
    env["movies"]["movie/7"]["monitored"] = monitored
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", grab=False)
    silent(env, monkeypatch, env["path"])
    hook.main([])
    assert env["writes"] == [("DELETE", "moviefile/11", None)] + ([("PUT", "movie/editor", {"movieIds": [7], "monitored": True})] if monitored
                                                                  else []) + [("POST", "command", {"name": "RescanMovie", "movieId": 7})]
    assert os.path.exists(old) and not os.path.exists(rb) and regrabs_counted("radarr") == 1
    rec = decided(env)
    assert rec["reasons"][0] == "old_file_restored" and rec["restore"][0]["linked"] is True
    f, act = action(env)
    assert (act["code"], act["came"], hook.action_code(act)) == ("restored", ["Film A (1979) HDTV-720p.mkv"], "restored")


def test_an_empty_answer_at_the_monitored_read_stops_the_restore_before_any_write(env, monkeypatch):
    """Radarr answers the read of the monitored items with an empty body. The restore stops before the delete, so the
    broken import and its movie stay as they were."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", grab=False)
    silent(env, monkeypatch, env["path"])
    real_files, real_arr = hook.Radarr.files, hook.arr

    def files(self, owner, items, read=True):
        monkeypatch.setattr(hook, "arr", lambda app, p: b"" if p == "movie/7" else real_arr(app, p))
        try:
            return real_files(self, owner, items, read)
        finally:
            monkeypatch.setattr(hook, "arr", real_arr)
    monkeypatch.setattr(hook.Radarr, "files", files)
    hook.main([])
    assert env["writes"] == [] and os.path.exists(rb) and not os.path.exists(old)
    act = action(env)[1]
    assert (act["code"], act["manual"], act["step"]) == ("failed", True, "reading which items are monitored")


def test_restore_switched_off_is_the_plain_regrab(env, monkeypatch, settings):
    """RESTORE=false. A re-grab deletes and searches as before, and a manual import only alerts."""
    settings(restore=False)
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv")
    silent(env, monkeypatch, env["path"])
    hook.main([])
    assert [m for m, p, b in env["writes"]] == ["DELETE", "PUT", "POST"] and os.path.exists(rb) and not os.path.exists(old)
    assert "restore" not in decided(env) and hook.action_code(action(env)[1]) == "regrabbed"
    assert hook.restore_plans("radarr", {"file_id": "11", "deleted": old, "recycled": rb}, {}, {11}) == {}


def test_restore_switched_off_leaves_a_manual_import_alone(env, monkeypatch, settings):
    settings(restore=False)
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", grab=False)
    silent(env, monkeypatch, env["path"])
    hook.main([])
    assert env["writes"] == [] and os.path.exists(rb) and os.path.exists(env["path"])
    assert action(env)[1] == {"code": "no_grab", "name": "Radarr"}


@pytest.mark.parametrize("listed", [False, True])
def test_a_manual_import_restores_only_for_a_kind_regrab_lists(env, monkeypatch, settings, listed):
    """A manual import has no grab record, so its re-grab is a delete and a restore of the old file. A kind REGRAB leaves
    out never deletes or restores."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", grab=False)
    silent(env, monkeypatch, env["path"])
    settings(regrab={"audio", "video"} if listed else {"video"})
    hook.main([])
    assert bool(env["writes"]) == listed and os.path.exists(old) == listed and os.path.exists(rb) != listed, env["writes"]
    if not listed:
        assert os.path.exists(env["path"]) and action(env)[1] == {"code": "no_grab", "name": "Radarr"}


def test_a_bin_name_the_app_gave_to_another_file_is_never_restored(env, monkeypatch):
    """A same-name upgrade, and the bin copy goes between the plan and the delete (the bin emptied by hand). The
    app then recycles the broken file to that exact name. The bin file is not the one the plan checked, so it stays in
    the bin, and the plain re-grab runs."""
    name = os.path.basename(env["path"])
    old, rb = upgrade(env, monkeypatch, name)
    open(env["path"], "wb").write(b"new" * 300)
    silent(env, monkeypatch, env["path"])
    env["at_delete"] = lambda: os.remove(rb)
    hook.main([])
    assert open(rb, "rb").read() == b"new" * 300 and not os.path.exists(old)   # the broken file, where the old copy was
    assert [m for m, p, b in env["writes"]] == ["DELETE", "PUT", "POST"] and env["writes"][-1][1] == "history/failed/2101"
    (r,) = decided(env)["restore"]
    assert r["result"] == "not restored: the recycle bin copy changed since its check"
    assert hook.action_code(action(env)[1]) == "regrabbed"


@pytest.mark.parametrize("monitored", [True, False])
def test_a_manual_import_whose_old_file_does_not_come_back_gets_a_search(env, monkeypatch, monitored):
    """The bin copy goes inside the app's delete of the broken manual import. The item has no file then, and a
    manual import has no grab to mark failed, so the hook sends the app a search. The outcome is searched, and the title
    says re-grabbed. An API search grabs an unmonitored item too, so an item you had not
    monitored gets no search, and the title says only what was wrong."""
    env["movies"]["movie/7"]["monitored"] = monitored
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", grab=False)
    silent(env, monkeypatch, env["path"])
    env["at_delete"] = lambda: os.remove(rb)
    hook.main([])
    f, act = action(env)
    assert (act["came"], act["stayed"]) == ([], "the recycle bin copy changed since its check")
    if monitored:
        assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                                 ("POST", "command", {"name": "MoviesSearch", "movieIds": [7]})]
        assert act["code"] == "searched"
    else:
        assert env["writes"] == [("DELETE", "moviefile/11", None)]
        assert act["code"] == "deleted"
    assert hook.CONTENT_CODES["searched"] == hook.CONTENT_CODES["deleted"] == "wrong_content"


def test_a_manual_import_checks_its_plan_again_before_the_delete(env, monkeypatch):
    """The bin copy changes after the plan and before the delete. The import stays, as before the restore existed."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", grab=False)
    silent(env, monkeypatch, env["path"])
    real = hook.count_regrab
    def count(app):   # runs between the plan and the delete
        open(rb, "wb").write(b"another file")
        return real(app)
    monkeypatch.setattr(hook, "count_regrab", count)
    hook.main([])
    assert env["writes"] == [] and os.path.exists(env["path"]) and not os.path.exists(old)
    act = action(env)[1]
    assert (act["code"], act["came"], act["stayed"]) == ("no_grab", [], "its recycle bin copy or its path changed before the delete")


def test_a_manual_import_whose_second_check_disagrees_stays(env, monkeypatch):
    """The second check from scratch finds the audio clean. The import stays, the old file stays in the bin, and the
    cap counts nothing."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv", grab=False)
    env["ffmpeg_out"] = [SILENCE] * 3 + env["ffmpeg_out"] * 3   # certain first, clean from scratch
    hook.main([])
    assert env["writes"] == [] and os.path.exists(env["path"]) and os.path.exists(rb) and not os.path.exists(old)
    assert action(env)[1] == {"code": "unconfirmed"} and not hook.store.items("regrabs")


# --- the hook's own copy of a replaced file (KEEP_REPLACED) ---------------------------------------------

def keep_on(monkeypatch, tmp_path):
    """KEEP_REPLACED on, with the top of the mount at tmp_path/media. Returns replaced_root()."""
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_replaced=True, keep_days=7))
    monkeypatch.setattr(hook, "mount_top", lambda f: str(tmp_path / "media"))
    return str(tmp_path / "media" / hook.CFG.recycle_dir)


def kept_upgrade(env, monkeypatch, tmp_path):
    """The upgrade of upgrade(), with its grab first: the library holds the old file and its .en.srt, and the Grab
    event links both. The app's side of the import is left to the test. Returns (old path, its .en.srt, recycle bin
    path, the kept link of the old file)."""
    keep_on(monkeypatch, tmp_path)
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv")
    os.replace(rb, old)
    srt = old[:-4] + ".en.srt"
    open(srt, "w").write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    env["movies"]["movie/7"]["movieFile"] = {"id": 10, "path": old}
    monkeypatch.setenv("radarr_eventtype", "Grab")
    hook.main([])
    monkeypatch.setenv("radarr_eventtype", "Download")
    del env["movies"]["movie/7"]["movieFile"]   # the import replaced it
    (k,) = [r["kept"] for r in hook.kept_read() if "of" not in r]
    return old, srt, rb, k


@pytest.mark.parametrize("case", ["no bin", "bin emptied", "other volume", "bin not visible"])
def test_the_hooks_own_copy_comes_back_when_the_bin_has_none(env, monkeypatch, tmp_path, case):
    """The grab linked the old file and its subtitle. The app's bin has no copy the restore can use, so the old file and
    its subtitle come back from the hook's own copy, after the same checks a bin copy gets."""
    old, srt, rb, k = kept_upgrade(env, monkeypatch, tmp_path)
    if case == "other volume":   # the app copied both into its bin on another file system
        shutil.copy(old, rb)
        shutil.copy(srt, os.path.join(os.path.dirname(rb), os.path.basename(srt)))
        monkeypatch.setattr(hook, "volume", lambda p: 64 if "/.recycle/" in p else 63)
    os.remove(old)
    os.remove(srt)
    monkeypatch.setenv("radarr_deletedrecyclebinpaths", {"no bin": "", "bin emptied": rb, "other volume": rb,
                                                         "bin not visible": "/nonexistent/recycle/" + os.path.basename(old)}[case])
    silent(env, monkeypatch, env["path"])
    hook.main([])
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "command", {"name": "RescanMovie", "movieId": 7}), ("POST", "history/failed/2101", None)]
    assert open(old, "rb").read() == b"old" * 300 and open(srt).read().endswith("Hello\n") and not os.path.exists(k)
    rec = decided(env)
    (r,) = rec["restore"]
    assert (r["result"], r["kept"], r["recycle"], r["linked"]) == ("restored", True, k, True) and r["check"]["audio"]["certain"] is None
    assert [(x["file"], x["result"]) for x in r["extras"]] == [(srt, "restored")]
    act = action(env)[1]
    assert (act["came"], act["own_copy"], act["linked"]) == (["Film A (1979) HDTV-720p.mkv"], True, True)
    assert {a[a.index("-i") + 1] for a in env["ffmpeg"]} == {env["path"], k}   # the own copy got the audio check of a bin copy
    (kept,) = [x for x in hook.kept_read() if "of" not in x]
    assert kept["import"] == "a1b2c3d4"   # the import of its own grab claimed it


def test_a_usable_bin_copy_wins_over_the_hooks_own_copy(env, monkeypatch, tmp_path):
    old, srt, rb, k = kept_upgrade(env, monkeypatch, tmp_path)
    os.replace(old, rb)
    os.replace(srt, os.path.join(os.path.dirname(rb), os.path.basename(srt)))
    silent(env, monkeypatch, env["path"])
    hook.main([])
    (r,) = decided(env)["restore"]
    assert (r["result"], r["recycle"], "kept" in r) == ("restored", rb, False) and r["extras"][0]["result"] == "restored"
    assert os.path.exists(old) and os.path.exists(k)   # the own copy waits for its prune
    act = action(env)[1]
    assert (act["came"], act["own_copy"]) == (["Film A (1979) HDTV-720p.mkv"], False)


@pytest.mark.parametrize("by, why", [
    ("repair", "The hook's own copy is older, because the hook changed the file after the grab"),
    ("import", "The hook's own copy is older, because another import replaced the file after the grab"),
    ("gone", "The hook's own copy is gone"),
    ("replaced", "The hook's own copy is no longer the file the grab linked")])
def test_a_stale_own_copy_never_comes_back(env, monkeypatch, tmp_path, by, why):
    """After the grab, a header repair renamed a new file over the old path, or another download's import replaced
    it. The own copy then holds an older version. Or the copy went, or another file took its name. The plain re-grab
    runs and the plan names why."""
    old, srt, rb, k = kept_upgrade(env, monkeypatch, tmp_path)
    if by == "repair":   # repack() keeps the original, then renames its new file over the path
        hook.keep_original(old)
        open(old + ".tmp", "wb").write(b"repaired" * 300)
        os.replace(old + ".tmp", old)
    elif by == "import":
        hook.queue_job({"app": "radarr", "deleted": old, "download_id": "another1"})
        for n in hook.queued():
            hook.drop_job(n)
    elif by == "gone":
        os.remove(k)
    else:
        os.remove(k)
        open(k, "wb").write(b"old" * 300)
    os.remove(old)
    os.remove(srt)
    monkeypatch.setenv("radarr_deletedrecyclebinpaths", "")
    silent(env, monkeypatch, env["path"])
    hook.main([])
    assert [m for m, p, b in env["writes"]] == ["DELETE", "PUT", "POST"] and not os.path.exists(old) and os.path.exists(k) == (by != "gone")
    (r,) = decided(env)["restore"]
    assert r["result"] == f"not restored: the app kept no copy in its recycle bin. {why}"
    assert action(env)[1]["stayed"] == f"the app kept no copy in its recycle bin. {why}"
    if by in ("repair", "import"):
        assert {x["stale"] for x in hook.kept_read()} == {why.split("because ")[1]}   # the subtitle follows its video


def test_a_stale_extra_stays_out_and_its_video_comes_back(env, monkeypatch, tmp_path):
    """A sidecar fix rewrote the subtitle after the grab. The video comes back, and the older subtitle stays out."""
    old, srt, rb, k = kept_upgrade(env, monkeypatch, tmp_path)
    hook.keep_original(srt)
    open(srt, "w").write("retimed")
    os.remove(old)
    os.remove(srt)
    monkeypatch.setenv("radarr_deletedrecyclebinpaths", "")
    silent(env, monkeypatch, env["path"])
    hook.main([])
    (r,) = decided(env)["restore"]
    assert (r["result"], r["kept"], r["extras"]) == ("restored", True, []) and os.path.exists(old) and not os.path.exists(srt)


def test_the_import_claims_the_copy_of_a_grab_with_no_download_id(env, monkeypatch, tmp_path):
    """A grab with no download id matches by the old path. The first import that replaces the path claims it. A second
    import replaced the file the claim kept, so the claim goes stale. A grab of another download goes stale too. A
    change by the hook after the import leaves a claim alone."""
    keep_on(monkeypatch, tmp_path)
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.keep_grab("radarr", 7, "")
    env["clock"][0] += 1
    hook.keep_grab("radarr", 7, "d2")   # a later grab of the same file, in the next second
    hook.queue_job({"app": "radarr", "deleted": env["path"], "download_id": "x1"})
    hook.kept_replaced(env["path"])   # the hook repairs the new file of x1
    assert sorted((r["download_id"], r.get("import"), r.get("stale")) for r in hook.kept_read()) == [
        ("", "x1", None), ("d2", None, "another import replaced the file after the grab")]
    assert hook.kept_copy("radarr", env["path"], "x1")[0]["download_id"] == ""
    hook.queue_job({"app": "radarr", "deleted": env["path"], "download_id": ""})   # a manual import at the same path
    assert [r.get("stale") for r in hook.kept_read() if r["download_id"] == ""] == ["another import replaced the file after the grab"]
    assert hook.kept_copy("radarr", env["path"], "x1") == (None, "The hook's own copy is older, because another import replaced the file after "
                                                              "the grab")


def test_an_in_place_conversion_makes_the_own_copy_stale(env, monkeypatch, tmp_path):
    """A .mkv that holds MP4 converts under its own name with no kept original. Its grab link goes stale."""
    keep_on(monkeypatch, tmp_path)
    mp4_named_mkv(env)
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.keep_grab("radarr", 7, "d1")
    assert hook.convert("radarr", env["path"], hook.mkvmerge(env["path"]), os.stat(env["path"]), True, {"app_id": 7})[0] == "repacked"
    (r,) = hook.kept_read()
    assert r["stale"] == "the hook changed the file after the grab" and open(r["kept"], "rb").read() == b"x" * 1000


def test_a_season_pack_grab_links_each_file_once_with_its_extras(env, monkeypatch, tmp_path, capsys):
    """Sonarr names the episodes of a grab by season and number. A two-episode file is linked once, an episode with no
    file and a file of another episode give nothing. An extra is named like its video."""
    root = keep_on(monkeypatch, tmp_path)
    folder = tmp_path / "media" / "Show" / "Season 1"
    folder.mkdir(parents=True)
    e12, e4, e5 = (str(folder / n) for n in ("Show - s01e01-e02.mkv", "Show - s01e04.mkv", "Show - s01e05.mkv"))
    for p in (e12, e4, e5, e12[:-4] + ".en.srt", e12[:-4] + "-thumb.jpg", e12[:-4] + " Proper.nfo"):
        open(p, "w").write(p)
    season = [{"id": 100 + n, "episodeNumber": n, "episodeFileId": f} for n, f in ((1, 201), (2, 201), (3, 0), (4, 204), (5, 205))]
    api = {"episode?seriesId=5&seasonNumber=1": season,
           "episode?episodeIds=101&episodeIds=102&episodeIds=103&episodeIds=104": [dict(e, seriesId=5) for e in season[:4]],
           "episodefile/201": {"id": 201, "path": e12}, "episodefile/204": {"id": 204, "path": e4}}
    monkeypatch.setattr(hook, "arr", lambda app, p: api[p])
    for k in [k for k in os.environ if k.startswith("radarr_")]:
        monkeypatch.delenv(k)
    for k, v in (("sonarr_eventtype", "Grab"), ("sonarr_series_id", "5"), ("sonarr_release_seasonnumber", "1"),
                 ("sonarr_release_episodenumbers", "1,2,3,4"), ("sonarr_download_id", "pack1")):
        monkeypatch.setenv(k, v)
    hook.main([])
    assert capsys.readouterr().out == "arr-media-guard: Grab ok\n"
    recs = hook.kept_read()
    assert sorted((os.path.basename(r["old"]), os.path.basename(r.get("of") or "")) for r in recs) == [
        ("Show - s01e01-e02-thumb.jpg", "Show - s01e01-e02.mkv"), ("Show - s01e01-e02.en.srt", "Show - s01e01-e02.mkv"),
        ("Show - s01e01-e02.mkv", ""), ("Show - s01e04.mkv", "")]
    (stamp,) = os.listdir(root)
    for r in recs:
        assert r["kept"] == os.path.join(root, stamp, "Show", "Season 1", os.path.basename(r["old"])) and r["download_id"] == "pack1"
        assert os.stat(r["kept"]).st_ino == os.stat(r["old"]).st_ino == r["ino"]
    (line,) = log_lines(env)
    assert line["result"] == "kept_replaced" and len(line["kept"]) == 4 and "note" not in line


def test_a_grab_of_an_item_with_no_file_keeps_nothing(env, monkeypatch, tmp_path, capsys):
    root = keep_on(monkeypatch, tmp_path)
    grabbed(env, monkeypatch)
    monkeypatch.setenv("radarr_eventtype", "Grab")
    hook.main([])
    assert capsys.readouterr().out == "arr-media-guard: Grab ok\n"
    assert not os.path.exists(root) and hook.kept_read() == [] and not os.path.exists(hook.CFG.log)


def test_a_refused_link_keeps_nothing_of_that_file_and_logs_one_line(env, monkeypatch, tmp_path):
    """A hard link the file system refuses is never replaced by a copy, which could lose the race to the import."""
    keep_on(monkeypatch, tmp_path)
    open(env["path"][:-4] + ".en.srt", "w").write("x")
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    tries = []
    monkeypatch.setattr(hook.os, "link", lambda a, b: tries.append(a) or (_ for _ in ()).throw(OSError(hook.errno.EXDEV, "Invalid cross-device link")))
    assert hook.keep_grab("radarr", 7, "d1")["note"] == f"not kept: {env['path']}: Invalid cross-device link"
    assert os.listdir(tmp_path / "media" / hook.CFG.recycle_dir) == []   # the empty stamp folder went
    assert tries == [env["path"]] and hook.kept_read() == [] and [r["result"] for r in log_lines(env)] == ["not_kept_replaced"]


def test_a_grab_the_api_fails_logs_and_still_answers_ok(env, monkeypatch, tmp_path, capsys):
    keep_on(monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "arr", lambda app, p: (_ for _ in ()).throw(urllib.error.URLError("refused")))
    monkeypatch.setenv("radarr_eventtype", "Grab")
    hook.main([])
    assert capsys.readouterr().out == "arr-media-guard: Grab ok\n"
    (line,) = log_lines(env)
    assert (line["result"], line["note"]) == ("error", "the grab kept nothing: URLError: <urlopen error refused>")


def test_a_grab_leaves_the_prune_to_the_audit_and_a_record_stays_while_its_link_does(env, monkeypatch, tmp_path):
    """The app waits for its grab, so the grab never prunes the NAS. An old record whose link the prune has not removed
    yet stays, and its link never blocks an edit. An old record whose link is gone goes."""
    root = keep_on(monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "prune_originals", lambda root: pytest.fail("the grab pruned"))
    lib = tmp_path / "media" / "Film B (1980)"
    lib.mkdir()
    old, gone = str(lib / "Film B (1980).mkv"), os.path.join(root, "20000101T000000Z", "Film C (1981)", "c.mkv")
    open(old, "w").write("b")
    link = os.path.join(root, "20000101T000000Z", "Film B (1980)", "Film B (1980).mkv")
    os.makedirs(os.path.dirname(link))
    os.link(old, link)
    hook.store.put("kept", "replaced", [
        dict(app="radarr", old=old, kept=link, download_id="", time=env["clock"][0] - 8 * 86400, ino=os.stat(link).st_ino),
        dict(app="radarr", old="/m/c.mkv", kept=gone, download_id="", time=env["clock"][0] - 8 * 86400, ino=1)])
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.keep_grab("radarr", 7, "d1")
    assert sorted(os.listdir(root)) == ["20000101T000000Z", time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(env["clock"][0]))]
    assert sorted(r["old"] for r in hook.kept_read()) == sorted([old, env["path"]])
    assert hook.links(os.stat(old)) == 1


@pytest.mark.parametrize("days", [7, 0])
def test_the_nightly_audit_prunes_the_grab_links_too(env, monkeypatch, settings, tmp_path, capsys, days):
    """With KEEP_ORIGINALS_DAYS set to 0 after use, the audit removes every grab link folder, so no link stays for ever.
    It leaves the originals at 0, as every release before 1.7.0 did."""
    root = keep_on(monkeypatch, tmp_path)
    settings(keep_days=days)
    stamps = ["20000101T000000Z", time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(env["clock"][0] - 3600))]
    for r in (root, str(tmp_path / "media" / hook.CFG.keep_dir)):
        for n in stamps:
            os.makedirs(os.path.join(r, n))
    env["movies"]["rootfolder"] = [{"path": str(tmp_path / "media")}]
    open(hook.CFG.log, "w").close()
    hook.main(["--audit", "radarr", "--since", "24h"])
    left = [] if days == 0 else stamps[1:]
    assert os.listdir(root) == left and sorted(os.listdir(tmp_path / "media" / hook.CFG.keep_dir)) == (stamps if days == 0 else left)
    assert f"removed {2 - len(left)} kept folders older than {days} days under {root}" in capsys.readouterr().out


def show_mount(monkeypatch, tmp_path):
    """A show on its own mount below the root folder tmp_path/media, as a dataset per show. Returns its folder and its
    RECYCLE_DIR. The rest of tmp_path/media is one mount."""
    show = tmp_path / "media" / "Show (2001)"
    show.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hook, "mount_top", lambda f: str(show if f == str(show) or f.startswith(str(show) + "/") else tmp_path / "media"))
    return show, str(show / hook.CFG.recycle_dir)


def test_the_audit_prunes_the_grab_links_of_a_mount_below_a_root_folder(env, monkeypatch, tmp_path):
    """The root folders name only the top mount. A record names the show's own mount, so its links are pruned too."""
    keep_on(monkeypatch, tmp_path)
    show, root = show_mount(monkeypatch, tmp_path)
    old = os.path.join(root, "20000101T000000Z", "Show (2001)", "e1.mkv")
    os.makedirs(os.path.dirname(old))
    open(old, "w").write("x")
    hook.store.put("kept", "replaced", [dict(app="sonarr", old=str(show / "e1.mkv"), kept=old, download_id="",
                                                                         time=env["clock"][0] - 8 * 86400, ino=os.stat(old).st_ino)])
    env["movies"]["rootfolder"] = [{"path": str(tmp_path / "media")}]
    open(hook.CFG.log, "w").close()
    hook.main(["--audit", "radarr", "--since", "24h"])
    assert os.listdir(root) == []


def test_the_worker_prunes_the_grab_links_once_a_day(env, monkeypatch, tmp_path):
    """A host with no nightly audit still prunes. The worker of an import does it at its start, once a day, off the
    app's call path."""
    keep_on(monkeypatch, tmp_path)
    show, root = show_mount(monkeypatch, tmp_path)
    old = os.path.join(root, "20000101T000000Z", "Show (2001)", "e1.mkv")
    os.makedirs(os.path.dirname(old))
    hook.store.put("kept", "replaced", [dict(app="sonarr", old=str(show / "e1.mkv"), kept=old, download_id="",
                                                                         time=env["clock"][0] - 8 * 86400, ino=1)])
    hook.main([])   # an import of Film A forks the worker
    assert decided(env) and os.listdir(root) == []
    os.makedirs(os.path.join(root, "20000102T000000Z"))
    hook.main([])
    assert os.listdir(root) == ["20000102T000000Z"]   # one prune a day
    env["clock"][0] += 86400
    hook.main([])
    assert os.listdir(root) == []


def test_a_bin_under_another_mount_top_of_one_file_system_is_another_volume(env, monkeypatch, tmp_path):
    """Two bind mounts of one file system share st_dev, but a rename between them fails with EXDEV. The bin sits on
    its own mount, so the old file stays in it, and the plain re-grab runs."""
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mkv")
    silent(env, monkeypatch, env["path"])
    real, mount = os.path.ismount, str(tmp_path / "media" / ".recycle")
    monkeypatch.setattr(hook.os.path, "ismount", lambda p: p == mount or real(p))
    assert os.stat(rb).st_dev == os.stat(os.path.dirname(old)).st_dev and hook.volume(rb) != hook.volume(old)
    hook.main([])
    (r,) = decided(env)["restore"]
    assert r["result"] == "not restored: the recycle bin is on another volume, and a restore never copies" and os.path.exists(rb)


def test_a_root_folder_named_through_a_symlink_shares_the_mount_top_of_its_target(monkeypatch, tmp_path):
    """/tv points to /mnt/nas/tv, and the bin sits on /mnt/nas. A rename between them works, so they are one volume."""
    nas = tmp_path / "nas"
    (nas / "tv" / "Show A").mkdir(parents=True)
    (nas / "recycle").mkdir()
    os.symlink(nas / "tv", tmp_path / "tv")
    real = os.path.ismount
    monkeypatch.setattr(hook.os.path, "ismount", lambda p: p == str(nas) or real(p))
    assert hook.volume(str(tmp_path / "tv" / "Show A" / "a.mkv")) == hook.volume(str(nas / "recycle" / "a.mkv"))


def test_a_dry_line_never_prints_none_when_the_remux_planned_nothing(env):
    """remux_block() gives no block when the run planned no remux. The track's line then says why it stays."""
    rec = {"path": env["path"], "source": "backfill", "apply": False, "subremux": {"remove": ["s1"], "codes": [], "result": "subtitle remux failed: x"}}
    (f,) = hook.sub_findings(rec, {"s1": {"why": "the words differ"}}, ["s1"])
    assert (f["kind"], f["lines"][0]["code"], f["lines"][0]["gone"], f["lines"][0]["block"]) == ("submatch", "stays", True, None)
    text = hook.alert_line(f, "planned")
    assert "None" not in text and text.endswith("They're still in the file, because rewriting the file failed (x). --apply would turn their "
                                                "default and forced flags off."), text


def volume_root(monkeypatch, tmp_path, *blocked):
    """A Docker volume at tmp_path/vol as the mount top, and the folders blocked that the hook may not write, as for a
    uid that does not own them. A mode would not stop root. Returns the mount top."""
    top = tmp_path / "vol"
    top.mkdir(exist_ok=True)
    monkeypatch.setattr(hook, "mount_top", lambda f: str(top))
    real, no = os.access, {str(top / b) if b else str(top) for b in blocked}
    monkeypatch.setattr(hook.os, "access", lambda p, mode, **k: False if str(p) in no and mode & os.W_OK else real(p, mode, **k))
    return top


def test_a_mount_top_the_hook_cannot_write_keeps_in_the_highest_writable_folder(env, monkeypatch, settings, tmp_path):
    """The tester's layout: the volume root only root may write, a writable folder below it. The original and the grab
    link go there, on the file's mount, so each is a hard link of the file."""
    top = volume_root(monkeypatch, tmp_path, "")
    path = top / "Temp" / "Show" / "e1.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    settings(keep_days=7)
    assert (hook.originals_root(str(path)), hook.replaced_root(str(path))) == (str(top / "Temp" / hook.CFG.keep_dir), str(top / "Temp" / hook.CFG.recycle_dir))
    assert hook.keepable(str(path), os.stat(path)) is None
    kept = hook.keep_original(str(path))
    assert kept.startswith(str(top / "Temp" / hook.CFG.keep_dir) + "/") and kept.endswith("/Show/e1.mkv")
    assert (os.stat(kept).st_ino, os.stat(kept).st_dev) == (os.stat(path).st_ino, os.stat(path).st_dev)
    assert hook.kept_folders() == {str(top / "Temp" / hook.CFG.keep_dir)}


def test_a_folder_at_the_mount_top_wins_and_a_fallback_stays_where_it_is(env, monkeypatch, tmp_path):
    """An existing writable folder at the mount top keeps every install where it is, even when the top itself is not
    writable. A fallback folder that exists stays the choice when a higher folder becomes writable later."""
    top = volume_root(monkeypatch, tmp_path, "", "TV")
    path = top / "TV" / "Show" / "e1.mkv"
    path.parent.mkdir(parents=True)
    assert hook.originals_root(str(path)) == str(top / "TV" / "Show" / hook.CFG.keep_dir)
    os.mkdir(top / "TV" / "Show" / hook.CFG.keep_dir)   # the first run made it
    volume_root(monkeypatch, tmp_path, "")   # TV became writable
    assert hook.originals_root(str(path)) == str(top / "TV" / "Show" / hook.CFG.keep_dir)
    os.mkdir(top / hook.CFG.keep_dir)
    assert hook.originals_root(str(path)) == str(top / hook.CFG.keep_dir)


def test_a_folder_on_another_mount_than_the_file_never_takes_the_keep(env, monkeypatch, tmp_path):
    """data is on the local disk and writable, and data/tv is a symlink to a share. The mount top as written is the
    local one, and a keep there would be a copy on the system disk. The folder goes on the share instead."""
    local, nas = tmp_path / "local", tmp_path / "nas"
    (local / "data").mkdir(parents=True)
    (nas / "tv" / "Show").mkdir(parents=True)
    os.symlink(nas / "tv", local / "data" / "tv")
    real = os.path.ismount
    monkeypatch.setattr(hook.os.path, "ismount", lambda p: str(p) in (str(local), str(nas)) or real(p))
    path = local / "data" / "tv" / "Show" / "e1.mkv"
    assert hook.mount_top(os.path.dirname(path)) == str(local)
    assert hook.originals_root(str(path)) == str(local / "data" / "tv" / hook.CFG.keep_dir)
    assert hook.replaced_root(str(path)) == str(local / "data" / "tv" / hook.CFG.recycle_dir)


def test_the_folder_record_never_loses_a_folder(env, monkeypatch, tmp_path):
    """A folder that is missing for a while, as on a NAS that is down, stays in the record. A store that does not take
    the change keeps the record as it is, and the hook logs one line."""
    a, b = tmp_path / "a" / hook.CFG.keep_dir, tmp_path / "b" / hook.CFG.keep_dir
    a.mkdir(parents=True)
    hook.remember_folder(str(a))
    a.rmdir()
    hook.remember_folder(str(b))
    assert hook.kept_folders() == {str(a), str(b)}
    monkeypatch.setattr(hook.store, "put", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    hook.remember_folder(str(tmp_path / "c" / hook.CFG.keep_dir))
    assert hook.kept_folders() == {str(a), str(b)}
    (line,) = log_lines(env)
    assert line["result"] == "warning" and line["note"] == "the hook could not record the folder: OperationalError: database is locked"


def test_with_no_writable_folder_the_reason_names_the_mount_top(env, monkeypatch, tmp_path):
    top = volume_root(monkeypatch, tmp_path, "", "TV", "TV/Show")
    path = top / "TV" / "Show" / "e1.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    assert hook.originals_root(str(path)) == str(top / hook.CFG.keep_dir)
    assert hook.keepable(str(path), os.stat(path)) == f"{top} is not writable"


def test_the_prunes_reach_a_fallback_folder_below_a_root_folder(env, monkeypatch, settings, tmp_path, capsys):
    """The root folder and the mount top are not writable, so both folders sit in the show's folder. The audit finds
    neither from the root folders, so the folder record names them. Each prune clears only a folder of the two names."""
    top = volume_root(monkeypatch, tmp_path, "", "TV")
    settings(keep_replaced=True, keep_days=7)
    show = top / "TV" / "Show"
    show.mkdir(parents=True)
    path = show / "e1.mkv"
    path.write_bytes(b"x")
    hook.keep_original(str(path))
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": str(path)}
    hook.keep_grab("radarr", 7, "d1")
    other = tmp_path / "other"
    (other / "20000101T000000Z").mkdir(parents=True)
    hook.store.put("kept", "folders", sorted(hook.kept_folders() | {str(other)}))
    folders = [str(show / hook.CFG.keep_dir), str(show / hook.CFG.recycle_dir)]
    assert hook.kept_folders() == {*folders, str(other)}
    for f in folders:
        os.mkdir(os.path.join(f, "20000101T000000Z"))
    env["movies"]["rootfolder"] = [{"path": str(top / "TV")}]
    open(hook.CFG.log, "w").close()
    hook.main(["--audit", "radarr", "--since", "24h"])
    assert all("20000101T000000Z" not in os.listdir(f) and len(os.listdir(f)) == 1 for f in folders)
    assert os.listdir(other) == ["20000101T000000Z"]   # never a folder of another name
    os.mkdir(os.path.join(folders[0], "20000102T000000Z"))
    hook.prune_kept()   # the worker's daily prune reaches it too
    assert "20000102T000000Z" not in os.listdir(folders[0]) and os.listdir(other) == ["20000101T000000Z"]


def test_the_daily_prune_never_removes_kept_originals_at_keep_days_0(env, settings, tmp_path):
    """KEEP_ORIGINALS_DAYS 0 keeps each kept original until a person removes it. The grab links of the recycle folder
    still go."""
    settings(keep_days=0)
    folders = [tmp_path / hook.CFG.keep_dir, tmp_path / hook.CFG.recycle_dir]
    for f in folders:
        (f / "20000101T000000Z").mkdir(parents=True)
    hook.store.put("kept", "folders", sorted(map(str, folders)))
    hook.prune_kept()
    assert [os.listdir(f) for f in folders] == [["20000101T000000Z"], []]


def test_a_restore_makes_a_later_grab_link_of_its_path_stale(env, monkeypatch, tmp_path):
    """An RSS grab linked the broken upgrade before the hook found it broken. The re-grab puts the old file back over
    that path, so the later grab's link holds the deleted broken file. It must never come back."""
    keep_on(monkeypatch, tmp_path)
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.keep_grab("radarr", 7, "d2")   # links the broken file
    rb = str(tmp_path / "bin.mkv")
    open(rb, "wb").write(b"old" * 300)
    os.remove(env["path"])   # the app's delete of the broken file
    plan = {"back": [(env["path"], rb, hook.bin_sig(rb))], "not": [], "owner": 7, "label": "x", "want": None, "checks": {}, "extras": {},
            "new": env["path"], "kept": {}}
    assert hook.restore({}, {11: plan})[0]["result"] == "restored"
    hook.queue_job({"app": "radarr", "deleted": env["path"], "download_id": "d2"})   # d2's import replaces the old file
    assert hook.kept_copy("radarr", env["path"], "d2") == (None, "The hook's own copy is older, because the hook changed the file after the grab")


def test_a_renamed_file_still_finds_the_hooks_own_link(env, monkeypatch, tmp_path):
    """The app renamed the file after a grab that never imported. The link still counts as the hook's own."""
    keep_on(monkeypatch, tmp_path)
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.keep_grab("radarr", 7, "d1")
    new = env["path"][:-4] + " Proper.mkv"
    os.rename(env["path"], new)
    assert hook.links(os.stat(new)) == 1
    os.link(new, tmp_path / "client-copy.mkv")
    assert hook.links(os.stat(new)) == 2   # a download client's copy still counts


def test_an_error_after_the_first_link_removes_the_links_of_the_grab(env, monkeypatch, tmp_path):
    """No link may stay without its record, or it would count as a download client's copy."""
    root = keep_on(monkeypatch, tmp_path)
    open(env["path"][:-4] + ".en.srt", "w").write("x")
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.store.db()
    monkeypatch.setattr(hook.store, "put", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database or disk is full")))
    with pytest.raises(sqlite3.OperationalError):
        hook.keep_grab("radarr", 7, "d1")
    assert os.listdir(root) == [] and os.stat(env["path"]).st_nlink == 1


@pytest.mark.parametrize("age, kept", [(7 * 86400 - 1800, False), (7 * 86400 - 7200, True)])
def test_a_copy_near_its_prune_stays_out_of_a_plan(env, monkeypatch, tmp_path, age, kept):
    """The nightly prune could remove the copy after the app deleted the broken file. So a copy within an hour of
    KEEP_ORIGINALS_DAYS stays out."""
    keep_on(monkeypatch, tmp_path)
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": env["path"]}
    hook.keep_grab("radarr", 7, "d1")
    hook.queue_job({"app": "radarr", "deleted": env["path"], "download_id": "d1"})
    env["clock"][0] += age
    rec, why = hook.kept_copy("radarr", env["path"], "d1")
    assert bool(rec) == kept and why == (None if kept else "The hook's own copy is too close to its prune at KEEP_ORIGINALS_DAYS")


def test_a_file_the_api_lists_and_that_is_not_on_disk_logs_one_line(env, monkeypatch, tmp_path):
    """The import won the race to the grab."""
    keep_on(monkeypatch, tmp_path)
    gone = env["path"][:-4] + " HDTV-720p.mkv"
    env["movies"]["movie/7"]["movieFile"] = {"id": 11, "path": gone}
    assert hook.keep_grab("radarr", 7, "d1")["note"] == f"not kept: {gone}: the file is not on disk"
    assert hook.kept_read() == [] and [r["result"] for r in log_lines(env)] == ["not_kept_replaced"]


def test_an_anime_batch_across_a_season_matches_the_absolute_numbers(env, monkeypatch, tmp_path):
    """Sonarr maps an anime batch 12-14 to S01E12, S02E01 and S02E02, and sends season 1 with numbers 12,1,2."""
    root = keep_on(monkeypatch, tmp_path)
    folder = tmp_path / "media" / "Show"
    folder.mkdir()
    paths = {(s, e): str(folder / f"Show - s{s:02d}e{e:02d}.mkv") for s, e in ((1, 1), (1, 2), (1, 12), (2, 1), (2, 2))}
    for p in paths.values():
        open(p, "w").write(p)
    eps = [{"id": 100 * s + e, "seasonNumber": s, "episodeNumber": e, "absoluteEpisodeNumber": 11 * (s - 1) + e + (1 if s == 2 else 0),
            "episodeFileId": 1000 + 100 * s + e, "seriesId": 5} for s, e in paths]
    assert [e["absoluteEpisodeNumber"] for e in eps] == [1, 2, 12, 13, 14]
    api = {"episode?seriesId=5": eps, "episode?episodeIds=112&episodeIds=201&episodeIds=202": [e for e in eps if e["id"] in (112, 201, 202)],
           **{f"episodefile/{e['episodeFileId']}": {"path": paths[e["seasonNumber"], e["episodeNumber"]]} for e in eps}}
    monkeypatch.setattr(hook, "arr", lambda app, p: api[p])
    for k in [k for k in os.environ if k.startswith("radarr_")]:
        monkeypatch.delenv(k)
    for k, v in (("sonarr_eventtype", "Grab"), ("sonarr_series_id", "5"), ("sonarr_series_type", "Anime"), ("sonarr_release_seasonnumber", "1"),
                 ("sonarr_release_episodenumbers", "12,1,2"), ("sonarr_release_absoluteepisodenumbers", "12,13,14"), ("sonarr_download_id", "b1")):
        monkeypatch.setenv(k, v)
    hook.main([])
    assert sorted(os.path.basename(r["old"]) for r in hook.kept_read()) == ["Show - s01e12.mkv", "Show - s02e01.mkv", "Show - s02e02.mkv"]


# --- the conversion into Matroska (docs/design.md, "Conversion") -----------------------------------------

def mp4_import(env, monkeypatch, sidecars=(".en.srt", ".en.forced.srt")):
    """An MP4 import of Film A with its sidecars, file id 11 with a scene name. The fake Radarr takes a ManualImport:
    the movie lists the imported path as file 12, or 13 when it is the original again. Returns (mp4, mkv) paths. The
    hook's conversion of an import is on here."""
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, convert=True))
    mp4 = env["path"][:-4] + ".mp4"
    os.replace(env["path"], mp4)
    for ext in sidecars:
        with open(env["path"][:-4] + ext, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    env["probe"] = copy.deepcopy(NOT_MATROSKA["probe"])
    for i, t in enumerate(env["probe"]["tracks"]):
        t["id"] = i   # mkvmerge -J numbers the tracks
    old = {"id": 11, "path": mp4, "quality": {"quality": {"id": 3, "name": "WEBDL-1080p"}, "revision": {"version": 1}},
           "languages": [{"id": 1, "name": "English"}], "releaseGroup": "GRP", "sceneName": "Film.A.1979.1080p.NF.WEB-DL.DDP5.1.H.264-GRP",
           "indexerFlags": 0, "customFormatScore": 25}
    env["movies"]["movie/7"].update(monitored=True, path=os.path.dirname(mp4), movieFile={k: v for k, v in old.items() if k != "customFormatScore"})
    env["movies"]["moviefile/11"] = old   # movie/<id> leaves the score out, as Radarr 6.4 does
    monkeypatch.setenv("radarr_moviefile_path", mp4)
    monkeypatch.setenv("radarr_moviefile_id", "11")

    def app_imports(app, p, method, body):
        if p == "command" and body["name"] == "ManualImport" and env.get("app_takes", True):
            f = body["files"][0]
            env["movies"][f'movie/{f["movieId"]}']["movieFile"] = {"id": 13 if f["path"].endswith(".mp4") else 12, "path": f["path"]}
        if p == "moviefile/bulk":
            env["bulk"] = [dict(body[0], customFormatScore=25)]
    env["on_write"] = app_imports
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: env["writes"].append((method, p, body)) or env["on_write"](app, p, method, body)
                        or ({"id": 1, "status": env.get("import_status", "completed")} if p == "command" else env.get("bulk")))
    return mp4, env["path"]


def import_body(path, **kw):
    """The ManualImport command the hook sends for Film A: the old record's quality, languages and release group."""
    f = {"path": path, "quality": {"quality": {"id": 3, "name": "WEBDL-1080p"}, "revision": {"version": 1}}, "languages": [{"id": 1, "name": "English"}],
         "releaseGroup": "GRP", "indexerFlags": 0, "movieId": 7, **kw}
    return ("POST", "command", {"name": "ManualImport", "importMode": "auto", "files": [f]})


def test_an_mp4_import_is_converted_relinked_and_scanned_in_plex(env, monkeypatch, settings):
    mp4, mkv = mp4_import(env, monkeypatch)
    folder, sent = os.path.dirname(mkv), refreshes(env, monkeypatch)
    root = os.path.join(os.path.dirname(os.path.dirname(folder)), hook.CFG.keep_dir)
    settings(keep_days=7)   # a header repair keeps its original. A conversion keeps none.
    monkeypatch.setattr(hook, "originals_root", lambda p: root)
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["reasons"][0], rec["path"], rec["ids"]["file_id"]) == ("edited", "repacked", mkv, 12), rec
    assert rec["repack"]["relink"] == {"import": "completed", "file_id": 12, "listed": [mkv], "remonitored": [],
                                       "scene_name": "Film.A.1979.1080p.NF.WEB-DL.DDP5.1.H.264-GRP", "score": [25, 25]}
    assert [s["lang"] for s in rec["repack"]["sidecars"]] == ["en", "en"] and rec["repack"]["new_path"] == mkv
    assert sorted(os.listdir(folder)) == [os.path.basename(mkv)]   # the mp4, the sidecars, the held name and the temp file are gone
    remux = env["repacks"][0]
    assert remux[-3:] == ["--default-track-flag", "0:0", mkv[:-4] + ".en.srt"] and remux[remux.index("--track-order") + 1] == "0:0,0:1,0:2,1:0,2:0"
    assert remux[remux.index(mkv[:-4] + ".en.forced.srt") - 2:remux.index(mkv[:-4] + ".en.forced.srt")] == ["--forced-display-flag", "0:1"]
    # the ManualImport with the old record's values, the scene name back, then the rescan after the flag edit
    assert env["writes"] == [import_body(mkv), ("PUT", "moviefile/bulk", [{"sceneName": "Film.A.1979.1080p.NF.WEB-DL.DDP5.1.H.264-GRP", "id": 12}]),
                             ("POST", "command", {"name": "RescanMovie", "movieId": 7})]
    assert env["mkvpropedit"] and [r["result"] for r in log_lines(env)][:2] == ["converting", "repacked"]
    assert analyzes(env) == [] and [(u, q["path"]) for m, u, q in sent] == [("/library/sections/12/refresh", [folder])]   # a folder scan
    assert not os.path.exists(os.path.join(hook.CFG.state_dir, "convert-radarr.txt"))   # the list holds refused and failed files only
    assert not os.path.exists(root) and "kept" not in rec["repack"]


def scanned_as_extras(env, folder, video):
    """The app's disk scan while mkvmerge writes, as another job's rescan runs it: every file beside the video becomes
    an extra of its record, file 11. A hidden file counts too. A hidden folder does not (ExcludedSubFoldersRegex)."""
    rows = []
    env["during_repack"] = lambda: rows.extend((n, 11, "other") for n in sorted(os.listdir(folder))
                                               if os.path.isfile(os.path.join(folder, n)) and n != os.path.basename(video))
    env["extra_rows"] = lambda: list(rows)
    return rows


def test_a_rescan_during_the_remux_never_takes_the_temp_file_as_an_extra(env, monkeypatch):
    """A real import: a rescan of the series ran while mkvmerge wrote the temp file beside the video. The app took the
    hidden temp file as an extra of the old record. The swap then hid it with the extras, and the link to the new name
    failed with ENOENT. The temp file now sits in HIDE_DIR, which the scan skips."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    rows = scanned_as_extras(env, os.path.dirname(mkv), mp4)
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["repack"]["extras_hidden"], rows) == ("edited", 0, []), rec["result"]
    assert env["repacks"][0][env["repacks"][0].index("-o") + 1] == hook.repack_tmp(mp4) == os.path.join(
        os.path.dirname(mp4), hook.CFG.hide_dir, "." + os.path.basename(mp4) + ".repack-tmp")
    assert sorted(os.listdir(os.path.dirname(mkv))) == [os.path.basename(mkv)]   # no temp file, no held name, no hidden folder


def test_a_failed_swap_removes_its_own_temp_file_after_the_extras_are_back(env, monkeypatch):
    """The failure path of the same import. The app listed the temp file as an extra, the swap hid it, and the link
    failed. The failure path removed the temp file before the extras went back, so the temp file came back as a
    second copy of the new file. It goes last now."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    folder = os.path.dirname(mkv)
    env["extra_rows"] = lambda: [(os.path.relpath(hook.repack_tmp(mp4), folder), 11, "other"), (os.path.basename(mkv)[:-4] + ".nfo-orig", 11, "other")]
    open(mkv[:-4] + ".nfo-orig", "w").close()
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and "No such file or directory" in rec["result"] and rec["repack"]["extras_left"] == [], rec
    assert sorted(os.listdir(folder)) == sorted([os.path.basename(mp4), os.path.basename(mkv)[:-4] + ".nfo-orig"])
    assert env["writes"] == [] and open(mp4, "rb").read() == b"x" * 1000


# The damage messages in the shape the remux, the proof and ffprobe log them. Every path and number is made up.
def invalid_audio(at="00:04:21.517000000", at2="00:04:21.541000000", track=1):
    """mkvmerge's two warnings for invalid data it skipped in an audio track, 1 by default, at those times of the 600 s file."""
    return "\n".join(f"Warning: '/m/Show/Season 1/Show - s01e02 - Title - DVD.avi' track {track}: This audio track contains {n} bytes of invalid "
                     f"data which were skipped before timestamp {t}. The audio/video synchronization may have been lost."
                     for n, t in ((173, at), (239, at2)))


INVALID_AUDIO = invalid_audio()
BAD_READ = ("NAL unit size (0 > 4817).\n[filter_units @ 0x55e3a1c07d40] Failed to read packet.\n[vost#0:0/copy @ 0x55e3a1c08e80] "
            "Error applying bitstream filters to a packet: Invalid data found when processing input")
NO_STREAM = "ffprobe read no stream: /m/Film A (1979)/Film A (1979).mp4: Invalid data found when processing input"
PARTIAL = "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x55d0c1a2b3c0] stream 1, offset 0x1f4a2b3: partial file"   # a download cut short


REFUSAL = "the packet data of stream audio 1 (mp3) differ"   # the proof after mkvmerge skipped invalid audio data
REFUSED = (1, REFUSAL)   # as prove() returns it, with the stream it refuses


def damaged_import(env, monkeypatch, signal, repeat=True, message=BAD_READ, refusal=REFUSED, second=None):
    """An MP4 import of Film A with a grab record, whose conversion shows the damage signal. mkvmerge's invalid-data
    warning comes with the proof's refusal. The second check from scratch finds the signal again when repeat. For
    mkvmerge, second is what its second run says instead. REGRAB lists damage. Returns (mp4, each read that showed it)."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    grabbed(env, monkeypatch)
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, regrab=set(hook.REGRAB_KINDS)))
    reads = []
    if signal == "mkvmerge":
        outs = iter([INVALID_AUDIO, second if second is not None else INVALID_AUDIO if repeat else ""])
        env["during_repack"] = lambda: env.update(repack_out=next(outs)) or reads.append(env["repacks"][-1])
        env["repack_rc"], env["proof"] = 1, (refusal, [])
    elif signal == "ffmpeg":
        def bad_read(path, maps, bsf, *a, **k):
            reads.append((path, bsf))
            if len(reads) == 1 or repeat:
                raise hook.ReadFailed(path, f"ffmpeg did not read {os.path.basename(path)} cleanly: {message}")
            return {}, {}
        monkeypatch.setattr(hook, "prove", lambda src, *a, **k: bad_read(src, [0, 1], {0: "filter_units=remove_types=9"}))
        monkeypatch.setattr(hook, "packet_hashes", bad_read)
    else:
        real, calls = hook.ff_streams, []
        def no_stream(path):
            calls.append(path)
            if len(calls) == 1 or repeat:
                raise RuntimeError(NO_STREAM)
            return real(path)
        monkeypatch.setattr(hook, "ff_streams", no_stream)
        reads = calls
    return mp4, reads


@pytest.mark.parametrize("signal, line, message", [
    ("mkvmerge", "This audio track contains 173 bytes of invalid data which were skipped before timestamp 00:04:21.517000000. The "
                 "audio/video synchronization may have been lost.", None),
    ("ffmpeg", "NAL unit size (0 > 4817).", BAD_READ),
    ("ffmpeg", "partial file", PARTIAL),
    ("ffprobe", NO_STREAM, None),
])
def test_a_damaged_source_re_grabs_the_import(env, monkeypatch, signal, line, message):
    """Each damage signal of a conversion deletes the import, re-monitors it and marks the grab failed, after a second
    read of the original from scratch finds the same signal. One red embed says so."""
    mp4, reads = damaged_import(env, monkeypatch, signal, message=message)
    hook.main([])
    rec = decided(env)
    fault = hook.DAMAGE[signal][1]
    assert (rec["outcome"], rec["regrab"]) == ("damaged_source", "regrabbed"), rec
    assert rec["repack"]["damage"] == dict({"read": signal, "fault": fault, "line": line},
                                           **({"refusal": REFUSAL, "stream": 1} if signal == "mkvmerge" else {}))
    assert env["writes"] == [("DELETE", "moviefile/11", None), ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}),
                             ("POST", "history/failed/2101", None)]
    assert env["mkvpropedit"] == [] and "audio" not in rec and len(reads) == 2   # the read that showed it, then the second check
    if signal == "mkvmerge":   # the same remux into /dev/null, which writes nothing
        assert reads[1][reads[1].index("-o") + 1] == os.devnull and reads[1][-1] == mp4
    if signal == "ffmpeg":   # the proof's read of the original, through the same bitstream filter
        assert reads[1] == (mp4, {0: "filter_units=remove_types=9"})
    f, act = action(env)
    assert rec["findings"] == [f] and f == {"kind": "damage", "fault": fault, "line": line, "refusal": REFUSAL if signal == "mkvmerge" else None,
                                            "action": act}
    assert act == {"code": "regrabbed", "name": "Radarr", "kind": "damage", "n": 1, "came": [], "linked": True, "own_copy": True, "others": 0,
                   "stayed": None}
    assert regrabs_counted("radarr") == 1


@pytest.mark.parametrize("case", ["timestamps", "edit list", "cues", "end skip", "start skip", "temp file read"])
def test_a_format_refusal_keeps_the_original(env, monkeypatch, settings, case):
    """mkvmerge skipped invalid audio data in each case. A refusal of another stream, or of the times, is no damage. A
    skip near an end of the file is end junk, even when the proof refuses that audio stream. A failed read of the temp
    file says nothing of the original. Each stays a refusal with its Repack failed embed, and the original stays, with
    damage in REGRAB."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    grabbed(env, monkeypatch)
    settings(regrab=set(hook.REGRAB_KINDS))
    env["repack_rc"], env["repack_out"] = 1, {"end skip": invalid_audio("00:09:58.806000000", "00:09:58.832000000"),
                                              "start skip": invalid_audio("00:00:00.412000000", "00:00:00.438000000")}.get(case, INVALID_AUDIO)
    env["proof"] = ({"timestamps": (None, "a packet of stream video 0 (h264) moved 27 ms against its stream's start, 842.516 s into the original"),
                     "edit list": (0, "stream video 0 (h264) holds 30918 packets in the new file, 30920 in the original"),
                     "cues": (None, "the mov_text stream 2 holds 471 cues in the new file, 483 in the original")}.get(case, REFUSED), [])
    if case == "temp file read":
        tmp = os.path.basename(hook.repack_tmp(mp4))
        monkeypatch.setattr(hook, "prove", lambda *a, **k: (_ for _ in ()).throw(hook.ReadFailed(hook.repack_tmp(mp4), f"ffmpeg did not read {tmp} cleanly: {BAD_READ}")))
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and "regrab" not in rec and os.path.exists(mp4), rec
    assert "damage" not in rec["repack"] and "history/failed/2101" not in str(env["writes"])
    assert rec["alert_kinds"] == ["repack"]
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1


def test_a_conversion_whose_original_changed_takes_the_lock_again_before_the_checks(env, monkeypatch):
    """The app replaced the original during the remux of an import. convert() let the file lock go, and the import goes
    on with its checks of the original, so process() takes the lock exclusive again first."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    def import_upgrade():
        with open(mp4 + ".part", "wb") as f:
            f.write(b"u" * 2000)
        os.replace(mp4 + ".part", mp4)
    env["during_repack"] = import_upgrade
    order, convert, gated, check = [], hook.convert, hook.gated, hook.check_audio
    monkeypatch.setattr(hook, "convert", lambda *a, **k: order.append("convert") or convert(*a, **k))
    monkeypatch.setattr(hook, "gated", lambda f, op, wait=hook.DEADLINE: order.append(op) or gated(f, op, wait))
    monkeypatch.setattr(hook, "check_audio", lambda *a, **k: order.append("audio") or check(*a, **k))
    hook.main([])
    assert decided(env)["outcome"] == "repack_source_changed"
    assert order[order.index("convert"):] == ["convert", fcntl.LOCK_EX, "audio"], order


def test_a_damaged_source_re_grab_of_an_upgrade_puts_the_old_file_back(env, monkeypatch):
    """The conversion of an upgrade shows its source damaged. The re-grab deletes it, the old file comes back from the
    recycle bin, and the alert says so."""
    mp4, reads = damaged_import(env, monkeypatch, "mkvmerge")
    env["path"] = mp4   # the import that upgrade() plays the app for
    old, rb = upgrade(env, monkeypatch, "Film A (1979) HDTV-720p.mp4")
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"], rec["reasons"][0]) == ("damaged_source", "regrabbed", "old_file_restored"), rec
    assert open(old, "rb").read() == b"old" * 300 and not os.path.exists(rb)
    f, act = action(env)
    assert (f["kind"], hook.action_code(act), act["came"]) == ("damage", "restored", ["Film A (1979) HDTV-720p.mp4"])


def test_a_damaged_source_only_says_it_would_regrab_by_default(env, monkeypatch, settings):
    """REGRAB leaves out damage by default. The hook checks the original again, keeps it, and alerts "would re-grab"."""
    mp4, reads = damaged_import(env, monkeypatch, "mkvmerge")
    settings(regrab={"audio", "video"})
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"]) == ("damaged_source", "would_regrab") and env["writes"] == [] and os.path.exists(mp4), rec
    f, act = action(env)
    assert len(reads) == 2 and rec["findings"][0] == f   # the second check ran
    assert (f["kind"], act) == ("damage", {"code": "would_regrab", "kind": "damage"})


@pytest.mark.parametrize("broken", ["audio", "video"])
def test_a_kind_regrab_leaves_out_only_alerts(env, monkeypatch, settings, broken):
    if broken == "video":
        env["window_out"] = [BAD_WINDOW]
    else:
        env["ffmpeg_out"] = [SILENCE]
    grabbed(env, monkeypatch)
    settings(regrab=set(hook.REGRAB_KINDS) - {broken})
    hook.main([])
    rec = log_lines(env)[0]
    f, act = action(env)
    assert env["writes"] == [] and os.path.exists(env["path"]) and rec["findings"][0] == f
    assert (f["kind"], act) == (broken, {"code": "would_regrab", "kind": broken})
    assert not hook.store.items("regrabs")   # nothing counted


def test_the_worker_logs_a_setting_it_cannot_read(env, monkeypatch, settings):
    settings(errors=["REGRAB names sound, which is no re-grab kind, so it is left out."])
    hook.main([])
    assert {"result": "warning", "note": "REGRAB names sound, which is no re-grab kind, so it is left out."}.items() <= log_lines(env)[0].items()


@pytest.mark.parametrize("serve", [False, True])
def test_the_worker_names_the_keys_from_the_environment(env, monkeypatch, settings, serve):
    """A host hook reads the app's environment, so a stray LOG there moves the decision log. The worker names such keys,
    never their values. The listener's worker reads the compose file's environment, and names none."""
    settings(from_env=("LOG", "RADARR_API_KEY"))
    monkeypatch.setattr(hook, "SERVE", serve)
    hook.main([])
    notes = [r["note"] for r in log_lines(env) if r.get("result") == "warning"]
    assert notes == ([] if serve else ["keys from the environment win over the env file: LOG, RADARR_API_KEY"]), notes


def test_the_cap_stops_a_damaged_source_re_grab(env, monkeypatch):
    mp4, reads = damaged_import(env, monkeypatch, "mkvmerge")
    hook.store.put("regrabs", "radarr", [env["clock"][0] - 60] * hook.CFG.regrab_cap)
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"]) == ("damaged_source", "capped") and env["writes"] == [] and os.path.exists(mp4), rec
    assert rec["findings"][0]["action"] == {"code": "capped", "cap": hook.CFG.regrab_cap}
    assert "audio" in rec and "video" in rec   # the original stays, so it gets the import's checks


def test_a_packet_count_refusal_of_the_warned_stream_re_grabs(env, monkeypatch):
    """The proof's refusal as it reads in full, after mkvmerge skipped invalid data in that same audio stream."""
    mp4, reads = damaged_import(env, monkeypatch, "mkvmerge", refusal=(1, "stream audio 1 (mp3) holds 1000 packets in the new file, 1003 in the original"))
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"], rec["repack"]["damage"]["stream"]) == ("damaged_source", "regrabbed", 1), rec
    assert ("DELETE", "moviefile/11", None) in env["writes"]


@pytest.mark.parametrize("case, refusal, second, regrab", [
    ("another stream refused", (3, "the packet data of stream audio 3 (aac) differ"), None, None),
    ("second check on another stream", REFUSED, invalid_audio(track=3), "unconfirmed"),
    ("second check at an end", REFUSED, invalid_audio("00:09:58.806000000", "00:09:58.832000000"), "unconfirmed"),
])
def test_invalid_audio_counts_only_for_the_refused_stream_away_from_the_ends(env, monkeypatch, case, refusal, second, regrab):
    """A file with two audio streams, 1 and 3. mkvmerge skipped invalid data in stream 1 at 4:21. A refusal of stream 3
    is no damage. The second check must find a skip in stream 1 away from the ends again."""
    mp4, reads = damaged_import(env, monkeypatch, "mkvmerge", refusal=refusal, second=second)
    dub = copy.deepcopy(env["probe"]["tracks"][1])
    dub["id"], dub["properties"]["uid"] = 3, 99
    env["probe"]["tracks"].append(dub)
    hook.main([])
    rec = decided(env)
    assert rec.get("regrab") == regrab and env["writes"] == [] and os.path.exists(mp4), rec
    assert rec["outcome"] == ("damaged_source" if regrab else "repack_failed")


def test_a_skip_in_a_file_of_unknown_length_is_no_damage(env, monkeypatch):
    """With no duration, no skip can be shown to lie away from the ends. The file stays."""
    mp4, reads = damaged_import(env, monkeypatch, "mkvmerge")
    monkeypatch.setattr(hook, "ffprobe_duration", lambda path: None)
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], "damage" in rec["repack"], env["writes"]) == ("repack_failed", False, []) and os.path.exists(mp4), rec


@pytest.mark.parametrize("stderr", ["", "/m/Film A (1979)/Film A (1979).mp4: Input/output error"])
def test_ffprobe_without_a_data_error_is_no_damage(env, monkeypatch, stderr):
    """ffprobe once failed with an empty message on a file it had read seconds before. A read error of the file system
    names no damage either. The conversion is skipped and the file is listed, as before."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    grabbed(env, monkeypatch)
    monkeypatch.setattr(hook, "ff_streams", lambda path: (_ for _ in ()).throw(RuntimeError(f"ffprobe read no stream: {stderr}")))
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_unreadable" and "damage" not in rec["repack"] and "regrab" not in rec, rec
    assert env["writes"] == [] and os.path.exists(mp4)


def test_the_damage_re_grab_judges_only_the_jobs_own_file(env, monkeypatch):
    """The download also imported file 12 of movie 8, which ffprobe cannot read either. Only the job's file goes. The
    other file shows its damage in its own conversion, and a line says it was not judged here."""
    mp4, reads = damaged_import(env, monkeypatch, "ffprobe")
    other = os.path.join(os.path.dirname(mp4), "Film B (1983).mp4")
    shutil.copy(mp4, other)
    env["movies"]["history?downloadId=a1b2c3d4&pageSize=1000"] = {"records": GRAB["records"] + [
        {"id": 2103, "eventType": "downloadFolderImported", "movieId": 8, "data": {"fileId": "12"}}]}
    env["movies"]["movie/8"] = {"title": "Film B", "movieFile": {"id": 12, "path": other}}
    hook.main([])
    assert [w for w in env["writes"] if w[0] == "DELETE"] == [("DELETE", "moviefile/11", None)] and os.path.exists(other)
    (w,) = [r for r in log_lines(env) if r.get("path") == other]
    assert w["note"] == "not judged for damaged source: only its own conversion reads it for damage" and other not in reads


def test_an_asf_remux_that_logs_a_message_keeps_the_original(env, monkeypatch):
    """mkvmerge cannot read ASF, so ffmpeg remuxes it. Only mkvmerge's warnings go to the proof. Any ffmpeg message fails
    the conversion, even when ffmpeg exits 0."""
    mp4_named_mkv(env)
    env["probe"] = copy.deepcopy(WMV)
    env["extra_streams"] = [{"index": 0, "codec_type": "video", "codec_name": "wmv2"}, {"index": 1, "codec_type": "audio", "codec_name": "wmav2"}]
    real = hook.subprocess.run
    def remux(argv, **kw):
        r = real(argv, **kw)
        if "matroska" in argv:
            r.stderr = "[asf @ 0x5610b2e4c1a0] Packet 3 exceeds the packet size"
        return r
    monkeypatch.setattr(hook.subprocess, "run", remux)
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and "ffmpeg exited 0: [asf @" in rec["result"] and "damage" not in rec["repack"], rec
    assert open(env["path"], "rb").read() == b"x" * 1000


def test_a_real_asf_conversion_overwrites_its_empty_temp_file(convertible, tmp_path, monkeypatch, settings):
    """new_tmp() creates the temp file before the remux, so ffmpeg must overwrite it. A .mkv name holding ASF converts in
    place, with the real tools and the real proof."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "Clip (2020)" / "Clip (2020).mkv"
    path.parent.mkdir()
    shutil.copy(convertible / "Clip.wmv", path)
    _, result, info, new = hook.convert("radarr", str(path), hook.mkvmerge(str(path)), os.stat(path), True, {"app_id": 7})
    assert (result, new) == ("repacked", str(path)), (result, info.get("warnings"))
    assert hook.mkvmerge(str(path))["container"]["type"] == "Matroska" and os.listdir(path.parent) == [path.name]


@pytest.mark.parametrize("out", [INVALID_AUDIO, "Warning: the timestamps of track 1 jump"])
def test_a_mkvmerge_warning_with_a_passing_proof_converts(env, monkeypatch, out):
    """mkvmerge exits 1 on warnings alone, as for zero bytes it skips at an audio end. The proof decides, and a clean
    proof converts. The warning stays in the log."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    grabbed(env, monkeypatch)
    env["repack_rc"], env["repack_out"] = 1, out
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["path"], rec["repack"]["warnings"]) == ("edited", mkv, out[-500:]), rec["result"]
    assert "damage" not in rec["repack"] and "history/failed/2101" not in str(env["writes"])


def test_a_backfill_lists_a_damaged_source_and_never_re_grabs(env, monkeypatch):
    """The library conversion finds the same damage. It only lists the file, and the original stays."""
    (mp4,) = mp4_films(env, monkeypatch, 1)
    env["repack_rc"], env["repack_out"], env["proof"] = 1, INVALID_AUDIO, (REFUSED, [])
    hook.main(["--backfill", "radarr", "--convert", "--apply"])
    (rec,) = [r for r in log_lines(env) if r.get("outcome")]
    assert (rec["outcome"], rec["repack"]["damage"]["read"]) == ("repack_failed", "mkvmerge") and "regrab" not in rec, rec
    assert os.path.exists(mp4) and env["writes"] == [] and [u for m, u, b in env["http"] if m == "POST"] == []
    (line,) = open(os.path.join(hook.CFG.state_dir, "convert-radarr.txt")).read().splitlines()
    assert line.split("\t")[1] == "repack_failed" and line.endswith(mp4)


@pytest.mark.parametrize("signal", ["mkvmerge", "ffmpeg", "ffprobe"])
def test_a_second_check_that_does_not_find_the_damage_again_keeps_the_file(env, monkeypatch, signal):
    mp4, reads = damaged_import(env, monkeypatch, signal, repeat=False)
    hook.main([])
    rec = decided(env)
    assert (rec["outcome"], rec["regrab"]) == ("damaged_source", "unconfirmed") and env["writes"] == [] and os.path.exists(mp4), rec
    f, act = action(env)
    assert (f["kind"], act) == ("damage", {"code": "unconfirmed"})


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_an_app_that_does_not_take_the_new_file_gets_the_original_back(env, monkeypatch, status):
    """The ManualImport fails, or completes and the movie still lists the MP4. The original goes back to its name. The
    app lists it already, so no second import runs. The import still gets its audio and video checks. The file goes on
    the list with one Repack failed embed."""
    mp4, mkv = mp4_import(env, monkeypatch)
    env["app_takes"], env["import_status"] = False, status
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and f"the app did not take the new file: import {status}, it lists [{mp4!r}]" in rec["result"], rec
    assert rec["repack"]["restored"] == {"listed": [mp4], "left": [], "result": "restored"} and env["writes"] == [import_body(mkv)]
    assert sorted(os.listdir(os.path.dirname(mkv))) == sorted(os.path.basename(mp4)[:-4] + e for e in (".mp4", ".en.srt", ".en.forced.srt"))
    assert "audio" in rec and "video" in rec and env["mkvpropedit"] == [] and rec["reasons"][0] == "not_matroska"
    (f,) = rec["findings"]
    assert f["kind"] == "repack" and f["why"].startswith("the app did not take the new file")
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1
    (line,) = open(os.path.join(hook.CFG.state_dir, "convert-radarr.txt")).read().splitlines()
    assert line.split("\t")[1:3] == ["repack_failed", "Film A (1979)"] and line.endswith(mp4)


def test_a_sidecar_timed_for_another_cut_stays_beside_the_file(env, monkeypatch):
    """A short file may come with sidecars that run to 38 minutes. Muxed in, they would stretch the file
    to 38 minutes, so they stay beside the new file, where the player still finds them by the base name. So does a
    sidecar whose cues are out of order."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=(".en.srt",))
    with open(mkv[:-4] + ".es.srt", "w") as f:
        f.write("1\n00:38:00,000 --> 00:38:27,000\nFin\n")
    with open(mkv[:-4] + ".en.forced.srt", "w") as f:   # its cues are out of order, so it stays beside the file
        f.write("1\n00:02:00,000 --> 00:02:01,000\nB\n\n2\n00:01:00,000 --> 00:01:01,000\nA\n")
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited" and [s["name"][-7:] for s in rec["repack"]["sidecars"]] == [".en.srt"], rec
    base = os.path.basename(mkv)[:-4]
    assert rec["repack"]["sidecars_left"] == [f"{base}.en.forced.srt: its cues are out of order",
                                              f"{base}.es.srt: its last cue ends at 38:27, the file at 10:00"]
    assert sorted(os.listdir(os.path.dirname(mkv))) == sorted([base + ".mkv", base + ".en.forced.srt", base + ".es.srt"])


@pytest.mark.parametrize("case, why", [("taken", "the name Film A (1979) WEBDL-1080p.mkv is taken"),
                                       ("proof", "repack failed: the packet data of stream video 0 (h264) differ"),
                                       ("other path", "the app lists /m/elsewhere.mp4, not this file"),
                                       ("outside", "the file sits outside the item's folder /m/Film A (1979), so a manual import would move it")])
def test_a_refused_conversion_keeps_the_original_and_its_sidecars(env, monkeypatch, case, why):
    mp4, mkv = mp4_import(env, monkeypatch)
    if case == "taken":
        open(mkv, "w").close()
    elif case == "proof":
        env["proof"] = ((0, "the packet data of stream video 0 (h264) differ"), [])
    elif case == "other path":
        env["movies"]["moviefile/11"]["path"] = "/m/elsewhere.mp4"
    else:
        env["movies"]["movie/7"]["path"] = "/m/Film A (1979)"
    hook.main([])
    rec = decided(env)
    assert why in rec["result"] and os.path.exists(mp4) and os.path.exists(mp4[:-4] + ".en.srt") and env["writes"] == [], rec
    assert not [n for n in os.listdir(os.path.dirname(mp4)) if n.startswith(".")]
    assert rec["outcome"] == {"taken": "repack_name_taken", "proof": "repack_failed"}.get(case, "repack_app_refused")


def test_an_m4v_with_cea608_captions_is_converted_with_an_english_cc_track(env, monkeypatch):
    """A c608 caption track beside AAC and H.264. The captions become the SubRip track "English
    (CC)", not default and not forced, after the kept tracks. The proof pairs it with the c608 stream."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=(".en.srt",))
    env["extra_streams"] = [{"index": 3, "codec_type": "subtitle", "codec_name": "eia_608"}]
    env["cc_text"] = CC_TEXT
    seen = []
    monkeypatch.setattr(hook, "prove", lambda src, tmp, subs, folder, captions=None: seen.append(captions) or copy.deepcopy(env["proof"]))
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited" and rec["repack"]["captions"] == [{"stream": 3, "cues": 3}], rec["result"]
    remux = env["repacks"][0]
    cc = remux.index("0:English (CC)")
    assert remux[cc - 3:cc + 8] == ["--language", "0:en", "--track-name", "0:English (CC)", "--sub-charset", "0:UTF-8", "--default-track-flag", "0:0",
                                    "--forced-display-flag", "0:0", seen[0][3]["path"]]
    assert remux.index(seen[0][3]["path"]) < remux.index(mkv[:-4] + ".en.srt")   # the captions, then the sidecars
    assert remux[remux.index("--track-order") + 1] == "0:0,0:1,0:2,1:0,2:0" and seen[0][3]["name"] == "English (CC)"


def test_an_m4v_whose_captions_give_no_text_keeps_the_original(env, monkeypatch):
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    env["extra_streams"] = [{"index": 3, "codec_type": "subtitle", "codec_name": "eia_608"}]
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_no_captions" and env["repacks"] == [] and os.path.exists(mp4), rec["result"]
    assert "audio" in rec and os.path.exists(os.path.join(hook.CFG.state_dir, "convert-radarr.txt"))


def sonarr_mp4(env, monkeypatch, link):
    """An MP4 of two Sonarr episodes, file 9 in the folder of series 5. link(episode ids, path) plays Sonarr's side of
    a ManualImport. Returns (mp4, mkv, the episodes, the files)."""
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, convert=True))
    mp4 = env["path"][:-4] + ".mp4"
    os.replace(env["path"], mp4)
    env["probe"] = copy.deepcopy(NOT_MATROSKA["probe"])
    series = {"title": "Show", "originalLanguage": {"name": "English"}, "path": os.path.dirname(mp4)}
    eps = [{"id": 31, "seasonNumber": 1, "episodeNumber": 2, "runtime": 22, "monitored": True, "episodeFileId": 9},
           {"id": 32, "seasonNumber": 1, "episodeNumber": 3, "runtime": 22, "monitored": True, "episodeFileId": 9}]
    files = {9: {"id": 9, "path": mp4, "quality": {"quality": {"id": 1}}, "languages": [{"id": 1}], "releaseGroup": "", "releaseType": "multiEpisode",
                 "indexerFlags": 0, "customFormatScore": 0}}
    as_sonarr(monkeypatch, env, series, eps)
    monkeypatch.setenv("sonarr_episodefile_path", mp4)

    def app(a, p):
        if p.startswith("episode?episodeIds") or p.startswith("episode?episodeFileId"): return eps
        if p.startswith("episodefile/"): return files[int(p.split("/")[1])]
        if p.startswith("parse?"):   # Sonarr's parse API: each name maps to both episodes, unless env["parse"] says other
            name = parse_qs(p.split("?", 1)[1])["title"][0]
            return {"episodes": [{"id": i} for i in env.get("parse", {}).get(name, [31, 32])]}
        if p.startswith("qualityprofile/"): return {}
        if p == "history/series?seriesId=5&eventType=3": return env.get("history", [])   # the imports, for a file's download path
        if p == "episodefile?seriesId=5": return list(files.values())
        return {"series/5": series}[p]
    monkeypatch.setattr(hook, "arr", app)

    def writes(a, p, method, body=None):
        env["writes"].append((method, p, body))
        if p == "command" and body["name"] == "ManualImport":
            f = body["files"][0]
            fid = max(files) + 1
            files[fid] = {"id": fid, "path": f["path"], "customFormatScore": 0}
            link(eps, f["episodeIds"], fid)
        return {"id": 1, "status": "completed"} if p == "command" else None
    monkeypatch.setattr(hook, "arr_write", writes)
    return mp4, env["path"], eps, files


def test_a_sonarr_import_is_converted_with_its_episode_ids_and_stays_monitored(env, monkeypatch):
    """The ManualImport names both episodes, so Sonarr never parses the new name. Its parser may read a name as other
    episodes. The episodes keep their monitored flag."""
    def link(eps, ids, fid):
        for e in eps:
            if e["id"] in ids: e["episodeFileId"] = fid
    mp4, mkv, eps, files = sonarr_mp4(env, monkeypatch, link)
    sent = refreshes(env, monkeypatch)
    hook.main([])
    rec = decided(env)
    assert rec["repack"]["relink"] == {"import": "completed", "file_id": 10, "listed": [mkv], "remonitored": [], "scene_name": None, "score": [0, 0]}, rec
    (m, p, body), rescan = env["writes"]
    assert body["files"] == [{"path": mkv, "quality": {"quality": {"id": 1}}, "languages": [{"id": 1}], "releaseGroup": "", "indexerFlags": 0,
                              "seriesId": 5, "episodeIds": [31, 32], "releaseType": "multiEpisode"}]
    assert rescan == ("POST", "command", {"name": "RescanSeries", "seriesId": 5}) and rec["ids"]["file_id"] == 10
    assert os.listdir(os.path.dirname(mkv)) == [os.path.basename(mkv)] and [q["path"] for m, u, q in sent] == [[os.path.dirname(mkv)]]


@pytest.mark.parametrize("case", ["dropped", "kept", "failed", "not hidden"])
def test_sonarr_settles_the_extras_once_a_completed_rescan_dropped_the_old_record(env, monkeypatch, case):
    """Sonarr 4 has no API for extra files. Its handler moves the extras of a dropped record to the bin, and deletes
    their rows, inside the command that drops the record (ExtraFileService, an IHandle). So the signal is a rescan that
    completed and no longer lists the old record. The hidden extras then come back at once. A rescan that failed, or
    one that kept the old record, keeps them hidden for the next run. The fake Sonarr plays the source: a rescan drops a
    record whose file is gone and moves each extra of it on disk to the bin. Kodi then writes its .nfo again. The .nfo
    is metadata, so it never hides. Sonarr also tracks a subtitle with another name and a bonus PDF in a sibling folder,
    because their names parse as the file's episodes, and both hide too. A subtitle of another episode stays where it
    is. Without the hiding, every extra of the old record goes to the bin."""
    def link(eps, ids, fid):
        for e in eps:
            if e["id"] in ids: e["episodeFileId"] = fid
    mp4, mkv, eps, files = sonarr_mp4(env, monkeypatch, link)
    folder, stem = os.path.dirname(mp4), os.path.basename(mp4)[:-4]
    os.mkdir(os.path.join(folder, "Bonus Material"))
    names = [stem + ".nfo", stem + ".pdf", stem + ".txt", "Bonus Material/Show S01E02 notes.pdf", "Show.S01E02.WEB.en.srt"]
    for n in names + ["Show S01E05.en.srt"]:   # a subtitle of another episode, which another file record holds
        with open(os.path.join(folder, n), "w") as f:
            f.write("<episodedetails></episodedetails>" if n.endswith(".nfo") else "1\n00:00:01,000 --> 00:00:02,000\nHi\n" if n.endswith(".srt") else "notes")
    env["parse"] = {"Show S01E05.en.srt": [40]}
    tracked, recycled, series_app, series_write = {9: list(names)}, [], hook.arr, hook.arr_write
    monkeypatch.setattr(hook, "arr", lambda a, p: [dict(f, seriesId=5) for f in files.values()] if p == "episodefile?seriesId=5" else series_app(a, p))

    def write(a, p, method, body=None):
        out = series_write(a, p, method, body)
        if p == "command" and body["name"] == "RescanSeries":
            for fid in [f for f, r in files.items() if not os.path.exists(r["path"]) and case != "kept"]:   # MissingFromDisk
                del files[fid]
                for n in tracked.pop(fid, []):
                    if os.path.exists(os.path.join(folder, n)):
                        recycled.append(n)
                        os.remove(os.path.join(folder, n))
                if not os.path.exists(os.path.join(folder, stem + ".nfo")):   # the Kodi writer, for the new file
                    with open(os.path.join(folder, stem + ".nfo"), "w") as f:
                        f.write("<episodedetails>new</episodedetails>")
            return {"id": 1, "status": "failed" if case == "failed" else "completed"}
        return out
    monkeypatch.setattr(hook, "arr_write", write)
    if case == "not hidden":
        monkeypatch.setattr(hook, "hide_extras", lambda paths: [])
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited" and rec["repack"]["relink"]["file_id"] == 10 and not os.path.exists(mp4), rec["result"]
    if case == "not hidden":
        assert sorted(recycled) == sorted(names) and "settle" not in rec["repack"] and os.path.exists(os.path.join(folder, "Show S01E05.en.srt"))
        return
    if case == "dropped":
        assert rec["repack"]["settle"] == {"rescan": "completed", "left": [], "rescan2": "completed", "extras": 4} and hook.pending_edit() == {}
        assert recycled == [stem + ".nfo"] and sorted(os.listdir(folder)) == sorted([os.path.basename(mkv), "Bonus Material", "Show S01E05.en.srt"]
                                                                                + names[:3] + names[4:])   # no hidden folder is left
        assert os.listdir(os.path.join(folder, "Bonus Material")) == ["Show S01E02 notes.pdf"]
        assert open(os.path.join(folder, stem + ".nfo")).read() == "<episodedetails>new</episodedetails>"
        return
    (key,) = hook.pending_edit()
    assert rec["repack"]["settle"] == {"rescan": "failed" if case == "failed" else "completed", "waiting": [key]}
    assert sorted(os.listdir(os.path.join(folder, hook.CFG.hide_dir))) == sorted(names[1:3] + names[4:]) and recycled == ([stem + ".nfo"] if case == "failed" else [])
    assert os.listdir(os.path.join(folder, "Bonus Material", hook.CFG.hide_dir)) == ["Show S01E02 notes.pdf"]


@pytest.mark.parametrize("name, eps", [("mkv", [7201]), ("pdf", [40]), ("pdf", [])])
def test_a_name_sonarr_reads_as_other_episodes_refuses_the_conversion(env, monkeypatch, name, eps):
    """The second rescan links each extra by the episodes its name parses as. The new name, and each
    extra that goes back, must parse as the file's own episodes, which an absolute numbering may not. A name that
    maps to other episodes, or to none, refuses the conversion. Nothing is written, and the file goes on the list."""
    def link(eps_, ids, fid):
        pytest.fail("no ManualImport may run")
    mp4, mkv, eps_, files = sonarr_mp4(env, monkeypatch, link)
    nfo = mp4[:-4] + ".pdf"   # an other extra, which goes back after the settle. Metadata does not.
    with open(nfo, "w") as f:
        f.write("<episodedetails/>")
    env["parse"] = {os.path.basename(mkv if name == "mkv" else nfo): eps}
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_parse_refused" and "Sonarr reads a name as other episodes" in rec["result"], rec["result"]
    assert sorted(os.listdir(os.path.dirname(mp4))) == sorted([os.path.basename(mp4), os.path.basename(nfo)]) and env["repacks"] == []
    assert env["writes"] == [] and open(nfo).read() == "<episodedetails/>"
    (line,) = open(os.path.join(hook.CFG.state_dir, "convert-sonarr.txt")).read().splitlines()
    assert line.split("\t")[1] == "repack_parse_refused"


@pytest.mark.parametrize("case", ["listed", "not listed", "proof refused", "proof refused, then logged", "listed, clean extra",
                                  "listed, extra mis-links"])
def test_a_listed_file_skips_the_sonarr_name_check(env, monkeypatch, settings, tmp_path, capsys, case):
    """Scene numbering can make Sonarr's parse read a right name as other episodes. A file a person lists with
    --force-convert skips that check, because the ManualImport names the item's own episodes by id. The decision line
    names the parse result in forced_name, the original stays in KEEP_DIR, and the audit says "name forced". An unlisted
    file still skips the conversion. The proof still runs: a listed file it refuses keeps its original, unless the
    decision log holds that refusal, as for any force. An extra keeps the check, because a rescan links it by its name:
    a listed file with a clean extra converts, and one whose extra maps to other episodes refuses and names the extra."""
    def link(eps, ids, fid):
        for e in eps:
            if e["id"] in ids: e["episodeFileId"] = fid
    mp4, mkv, eps, files = sonarr_mp4(env, monkeypatch, link)
    series_app = hook.arr   # sonarr_mp4()'s app, and the lists a backfill reads
    linked = lambda: sorted({e["episodeFileId"] for e in eps})   # the file the episodes link, 9 and then the new one
    lists = {"series": lambda: [{"id": 5, "title": "Show", "statistics": {"episodeFileCount": 1}}], "episode?seriesId=5": lambda: eps,
             "episodefile?seriesId=5": lambda: [dict(files[i], seriesId=5) for i in linked()]}
    monkeypatch.setattr(hook, "arr", lambda a, p: lists[p]() if p in lists else series_app(a, p))
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    settings(keep_days=7)
    root = tmp_path / hook.CFG.keep_dir
    monkeypatch.setattr(hook, "originals_root", lambda p: str(root))
    env["parse"] = {os.path.basename(mkv): [7201]}   # two episodes early
    extra = mp4[:-4] + ".pdf"
    if "extra" in case:   # an extra that goes back after the settle
        with open(extra, "w") as f:
            f.write("notes")
        env["parse"][os.path.basename(extra)] = [7201] if case.endswith("mis-links") else [31, 32]
    refusal = "stream video 0 (h264) holds 1006 packets in the new file, 1000 in the original"
    if case.startswith("proof refused"):
        env["proof"] = ((0, refusal), [])
    force = [] if case == "not listed" else ["--force-convert", mp4]
    hook.main(["--backfill", "sonarr", "--convert", "--apply", *force])
    if case == "proof refused, then logged":   # a person saw the refusal, and forces the file again
        capsys.readouterr()
        hook.main(["--backfill", "sonarr", "--convert", "--apply", *force])
    rec = [r for r in log_lines(env) if r.get("outcome")][-1]
    imports = [w[2] for w in env["writes"] if w[1] == "command" and w[2]["name"] == "ManualImport"]
    if case == "listed, extra mis-links":
        assert rec["outcome"] == "repack_parse_refused" and f"maps {os.path.basename(extra)} to other episodes" in rec["result"], rec
        assert imports == [] and os.path.exists(mp4) and os.path.exists(extra) and env["repacks"] == [] and "forced_name" not in rec["repack"]
        return
    if case in ("not listed", "proof refused"):
        assert rec["outcome"] == ("repack_parse_refused" if case == "not listed" else "repack_failed") and os.path.exists(mp4), rec
        assert imports == [] and not os.path.exists(mkv) and not [f for _, _, fs in os.walk(root) for f in fs]
        assert ("forced_name" in rec["repack"]) == (case == "proof refused") and "forced" not in rec["repack"]
        return
    assert rec["outcome"] == "repacked" and "Sonarr's parse maps" in rec["repack"]["forced_name"], rec
    assert [f["episodeIds"] for b in imports for f in b["files"]] == [[31, 32]] and {e["episodeFileId"] for e in eps} == {10}
    assert rec["repack"]["kept"].startswith(str(root) + "/") and os.path.exists(rec["repack"]["kept"]) and not os.path.exists(mp4)
    assert ("forced" in rec["repack"]) == (case == "proof refused, then logged")
    note = ", forced, name forced" if case == "proof refused, then logged" else ", name forced"
    assert hook.forced_note(rec["repack"]) == note
    assert rec["repack"]["forced_name"].startswith("Sonarr reads a name as other episodes")
    assert os.path.basename(mkv) in rec["repack"]["forced_name"] and os.path.basename(extra) not in rec["repack"]["forced_name"]
    assert os.path.exists(extra) == (case == "listed, clean extra")   # the extra goes back beside the new file


def test_a_sonarr_import_that_links_one_episode_of_two_is_undone(env, monkeypatch):
    """Sonarr links only episode 31 to the new file. The original goes back to its name, and a second ManualImport links
    both episodes to it again, with the same values."""
    def link(eps, ids, fid):
        for e in eps:
            if e["id"] in ids and (fid != 10 or e["id"] == 31): e["episodeFileId"] = fid
    mp4, mkv, eps, files = sonarr_mp4(env, monkeypatch, link)
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and rec["repack"]["relink"]["listed"] == sorted([mp4, mkv]), rec
    assert rec["repack"]["restored"]["import"]["file_id"] == 11 and rec["repack"]["restored"]["listed"] == [mp4, mp4]
    assert [w[2]["files"][0]["path"] for w in env["writes"] if w[1] == "command"] == [mkv, mp4] and {e["episodeFileId"] for e in eps} == {11}
    assert os.listdir(os.path.dirname(mp4)) == [os.path.basename(mp4)]


@pytest.mark.parametrize("score, ok", [(10, True), (3, False)])
def test_the_relink_gives_back_a_monitored_flag_the_app_cleared(monkeypatch, score, ok):
    """A ManualImport never unmonitors, but the hook checks anyway: Radarr may run with
    autoUnmonitorPreviouslyDownloadedMovies on. A scene name Radarr refuses (it has spaces) stays empty. A lower custom
    format score refuses the new file."""
    movie = {"monitored": True, "movieFile": {"id": 11, "path": "/m/a.mp4"}}
    writes = []

    def write(app, p, method, body=None):
        writes.append((method, p, body))
        if p == "command":
            movie.update(monitored=False, movieFile={"id": 12, "path": body["files"][0]["path"]})
            return {"id": 1, "status": "completed"}
        return [{"id": 12, "sceneName": None, "customFormatScore": score}]
    monkeypatch.setattr(hook, "arr_write", write)
    monkeypatch.setattr(hook, "arr", lambda app, p: movie)
    got, info = hook.relink("radarr", 7, {"id": 11, "sceneName": "Film A 1979 1080p", "customFormatScore": 10}, {7: True}, "/m/a.mkv")
    assert got is ok and info["score"] == [10, score] and info["file_id"] == 12
    assert info.get("refused") == (None if ok else "the custom format score dropped from 10 to 3")
    assert writes[-1] == ("PUT", "movie/editor", {"movieIds": [7], "monitored": True}) and info["remonitored"] == [7]


@pytest.mark.parametrize("film, lost, refused", [("Film H", "Format A", True), ("Film I", "Format C", False), ("Film J", "Format B", True)])
def test_a_name_that_loses_a_scoring_custom_format_refuses_the_conversion(env, monkeypatch, film, lost, refused):
    """With the shapes Radarr 6.4 gives. Film H has no scene name, and Radarr scores "Format A" (+5) on the
    name of its download path. That name has no release group, so Radarr would keep no scene name, and the new file
    name gives only "Format B". The check refuses before the remux, and the file goes on the list. Film I loses
    "Format C", which scores 0 in the profile, so it converts. Film J has a scene name with no release group,
    which Radarr's bulk update drops. Its new name then loses "Format B" (+2)."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    record = env["movies"]["moviefile/11"]
    fid = {"Format A": 1, "Format B": 2, "Format C": 3}[lost]
    record.update(sceneName="Film.J.1994.480p.TVRip.AAC.2.0.x264-47" if film == "Film J" else None,
                  customFormatScore={"Film H": 7, "Film I": 2, "Film J": 2}[film],
                  customFormats=[{"id": fid, "name": lost}, {"id": 2, "name": "Format B"}])
    assert "customFormatScore" not in env["movies"]["movie/7"]["movieFile"]   # movie/<id> leaves it out, as in Radarr 6.4
    download = {"Film H": "Film.H.2007.Extended.Cut.BluRay.H264.AC3.DD5", "Film I": "Film I (2012) 1080p WEBdl AVC EAC3 GRP",
                "Film J": "Film.J.1994.480p.TVRip.AAC.2.0.x264-47"}[film]
    record["originalFilePath"] = f"{download}/{download}.mp4"   # Radarr's API shows it on the record
    env["movies"]["movie/7"]["qualityProfileId"] = 4
    env["profile"] = {"formatItems": [{"format": 1, "name": "Format A", "score": 5}, {"format": 2, "name": "Format B", "score": 2},
                                      {"format": 3, "name": "Format C", "score": 0}]}

    def parse(title):   # Radarr's parse API: the old title gives the lost format. The new file name gives Format B, Film J's (SDTV) none.
        cf = ([] if film == "Film J" else [{"id": 2, "name": "Format B"}]) if title.endswith(".mkv") else [{"id": fid, "name": lost}]
        return {"customFormats": cf, "parsedMovieInfo": {"releaseGroup": None, "quality": {"quality": {"name": "Bluray-720p"}}}}
    env["parse_cf"] = parse
    env["movies"]["moviefile/12"] = {"id": 12, "path": mkv, "customFormatScore": 2, "customFormats": [{"id": 2, "name": "Format B"}]}
    hook.main([])
    rec = decided(env)
    if refused:
        assert rec["outcome"] == "repack_score_refused" and f"lost {lost} ({'+5' if film == 'Film H' else '+2'})" in rec["result"], rec["result"]
        assert env["repacks"] == [] and env["writes"] == [] and os.path.exists(mp4)
        (line,) = open(os.path.join(hook.CFG.state_dir, "convert-radarr.txt")).read().splitlines()
        assert line.split("\t")[1] == "repack_score_refused"
    else:
        assert rec["outcome"] == "edited" and os.path.exists(mkv), rec["result"]


def test_a_listed_file_still_refuses_at_the_score_check(env, monkeypatch, settings, tmp_path, capsys):
    """--force-convert overrides only a logged proof refusal and Sonarr's name check. A listed file whose new name would
    lose a custom format that scores still refuses before the remux, as Film H does without the option."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    record = env["movies"]["moviefile/11"]
    record.update(sceneName=None, customFormatScore=7, customFormats=[{"id": 1, "name": "Format A"}, {"id": 2, "name": "Format B"}])
    download = "Film.H.2007.Extended.Cut.BluRay.H264.AC3.DD5"
    record["originalFilePath"] = f"{download}/{download}.mp4"   # Radarr's API shows it on the record
    env["movies"]["movie/7"]["qualityProfileId"] = 4
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "movieId": 7, "path": mp4})]
    env["profile"] = {"formatItems": [{"format": 1, "name": "Format A", "score": 5}, {"format": 2, "name": "Format B", "score": 2}]}
    env["parse_cf"] = lambda title: {"customFormats": [{"id": 2, "name": "Format B"}] if title.endswith(".mkv") else [{"id": 1, "name": "Format A"}],
                                     "parsedMovieInfo": {"releaseGroup": None, "quality": {"quality": {"name": "Bluray-720p"}}}}
    settings(keep_days=7)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--force-convert", mp4])
    (rec,) = [r for r in log_lines(env) if r.get("outcome")]
    assert rec["outcome"] == "repack_score_refused" and "lost Format A (+5)" in rec["result"], rec["result"]
    assert env["repacks"] == [] and env["writes"] == [] and os.path.exists(mp4) and not os.path.exists(mkv)
    assert "name check" not in capsys.readouterr().out


def test_a_record_with_no_scene_name_keeps_its_download_name(monkeypatch):
    """Film H has no scene name, and the app scored "Format A" on its original download path. The new
    record has no such path, so the hook sends that path's name as the scene name. The app keeps it when it reads as a
    release name (SceneChecker.IsSceneTitle)."""
    movie = {"monitored": True, "movieFile": {"id": 11, "path": "/m/u.mp4"}}
    writes = []

    def write(app, p, method, body=None):
        writes.append((method, p, body))
        if p == "command":
            movie["movieFile"] = {"id": 12, "path": body["files"][0]["path"]}
            return {"id": 1, "status": "completed"}
        return [{"id": 12, "sceneName": body[0]["sceneName"], "customFormatScore": 7}]
    monkeypatch.setattr(hook, "arr_write", write)
    monkeypatch.setattr(hook, "arr", lambda app, p: movie)
    name = "Film.H.2007.UNRATED.1080p.BluRay.DTS-HD.MA.5.1-GRP"
    ok, info = hook.relink("radarr", 7, {"id": 11, "customFormatScore": 7, "originalFilePath": f"/downloads/{name}/{name}.mp4"}, {7: True}, "/m/u.mkv")
    assert ok and ("PUT", "moviefile/bulk", [{"sceneName": name, "id": 12}]) in writes and info["score"] == [7, 7]


def radarr_extras(env, monkeypatch, late_task=0):
    """Film A as an MP4 import with three extras Radarr tracks for file 11: a Spanish .srt timed for another cut,
    which stays beside the file, a .pdf and an .nfo. The fake Radarr plays the source of Radarr 6.4 on a RescanMovie: a
    file record whose file is gone goes. A task then moves its extras that exist to the recycle bin and deletes their
    rows (ExtraFileService, IHandleAsync). With late_task it runs only after that many more reads of the database, as a
    background task that outlives the rescan. The metadata writer writes the .nfo again for the movie's file. Then
    every extra on disk that no row names links to the movie's file, and a hidden folder is skipped
    (ExistingExtraFileService, DiskScanService). Returns (mp4, mkv, the extras, the rows, the recycled names)."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    folder, base = os.path.dirname(mp4), os.path.basename(mp4)[:-4]
    for ext, text in ((".es.srt", "1\n00:38:00,000 --> 00:38:27,000\nFin\n"), (".pdf", "%PDF"), (".nfo", "<movie/>")):
        with open(os.path.join(folder, base + ext), "w") as f:
            f.write(text)
    table = {".srt": "subtitle", ".pdf": "other", ".nfo": "metadata"}
    rows = [{"relativePath": base + e, "movieFileId": 11, "table": table[e[-4:]]} for e in (".es.srt", ".pdf", ".nfo")]
    records, recycled, movie, tasks = [{"id": 11, "path": mp4}], [], env["movies"]["movie/7"], []

    def recycle(fid):   # ExtraFileService.HandleAsync(MovieFileDeletedEvent)
        for x in [x for x in rows if x["movieFileId"] == fid]:
            if os.path.exists(os.path.join(folder, x["relativePath"])):
                recycled.append(x["relativePath"])
                os.remove(os.path.join(folder, x["relativePath"]))
            rows.remove(x)

    def read():   # the API read of Radarr.extra_rows(). A late task runs between two reads.
        for t in list(tasks):
            t[0] -= 1
            if t[0] < 0:
                tasks.remove(t)
                recycle(t[1])
        return [(x["relativePath"], x["movieFileId"], x["table"]) for x in rows]
    env["movies"]["moviefile?movieId=7"] = records
    env["extra_rows"] = read
    real = env["on_write"]

    def app(a, p, method, body):
        real(a, p, method, body)
        if p == "command" and body["name"] == "ManualImport":
            for r in [r for r in records if r["path"] == body["files"][0]["path"]]:   # ImportApprovedMovie: ManualOverride
                records.remove(r)
                tasks.append([late_task, r["id"]]) if late_task else recycle(r["id"])
            records.append(dict(movie["movieFile"]))
        if p == "command" and body["name"] == "RescanMovie":
            for r in [r for r in records if not os.path.exists(r["path"])]:
                records.remove(r)
                tasks.append([late_task, r["id"]]) if late_task else recycle(r["id"])
            nfo = base + ".nfo"
            if not os.path.exists(os.path.join(folder, nfo)):   # the Kodi metadata writer, for the movie's file
                with open(os.path.join(folder, nfo), "w") as f:
                    f.write("<movie new/>")
                rows.append({"relativePath": nfo, "movieFileId": movie["movieFile"]["id"], "table": "metadata"})
            known = {x["relativePath"] for x in rows}
            rows.extend({"relativePath": n, "movieFileId": movie["movieFile"]["id"], "table": table.get(n[-4:], "other")}
                        for n in sorted(os.listdir(folder)) if n.endswith((".srt", ".pdf")) and n not in known)
    env["on_write"] = app
    return mp4, mkv, [base + ".es.srt", base + ".pdf"], rows, recycled


@pytest.mark.parametrize("hide, late", [(True, 0), (False, 0), (True, 3)])
def test_the_extras_of_a_converted_file_stay_and_link_to_the_new_file(env, monkeypatch, hide, late):
    """The subtitle and the other extra wait in the hidden folder while a rescan
    drops the old record, then come back, and a second rescan links them to the new file. Without the hiding, the fake
    Radarr moves both to the recycle bin, as the real one does. The .nfo is never hidden: the app recycles it and its
    metadata writer writes it again. Radarr's recycle task may run after the rescan ended: the extras
    come back only once the database holds no row of the old record."""
    mp4, mkv, extras, rows, recycled = radarr_extras(env, monkeypatch, late)
    nfo = os.path.basename(mp4)[:-4] + ".nfo"
    if not hide:
        monkeypatch.setattr(hook, "hide_extras", lambda paths: [])
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited" and rec["repack"]["relink"]["file_id"] == 12, rec["result"]
    folder = os.path.dirname(mkv)
    if not hide:
        assert sorted(recycled) == sorted(extras + [nfo]) and "settle" not in rec["repack"]
        return
    assert recycled == [nfo] and sorted(os.listdir(folder)) == sorted([os.path.basename(mkv), nfo] + extras)   # no hidden folder is left
    assert open(os.path.join(folder, nfo)).read() == "<movie new/>"   # the metadata writer's new copy
    assert sorted((x["relativePath"], x["movieFileId"]) for x in rows) == sorted((x, 12) for x in extras + [nfo])
    assert rec["repack"]["extras_hidden"] == 2 and rec["repack"]["settle"] == {"rescan": "completed", "left": [], "rescan2": "completed", "extras": 2,
                                                                                "linked": 2, "not_linked": []}
    assert [w[2]["name"] for w in env["writes"] if w[1] == "command"] == ["ManualImport", "RescanMovie", "RescanMovie"]
    assert hook.pending_edit() == {}


def test_an_undo_waits_for_the_recycle_task_before_the_extras_come_back(env, monkeypatch):
    """The custom format score dropped after the import, so the original goes back into the app. That
    ManualImport drops the old record, and Radarr's recycle task runs a while later. The hidden extras come back only
    once the task deleted the old rows, and a rescan links them to the original's new record."""
    mp4, mkv, extras, rows, recycled = radarr_extras(env, monkeypatch, late_task=3)
    real = env["on_write"]

    def lower(app, p, method, body):
        real(app, p, method, body)
        if p == "moviefile/bulk":
            env["bulk"] = [dict(body[0], customFormatScore=5)]
    env["on_write"] = lower
    hook.main([])
    rec = decided(env)
    nfo = os.path.basename(mp4)[:-4] + ".nfo"
    assert rec["outcome"] == "repack_failed" and rec["repack"]["restored"]["result"] == "restored", rec["repack"].get("restored")
    assert rec["repack"]["restored"]["rows_left"] == 0 and rec["repack"]["restored"]["rescan"] == "sent"
    assert recycled == [nfo] and sorted(os.listdir(os.path.dirname(mp4))) == sorted([os.path.basename(mp4), nfo] + extras)   # the writer's new .nfo
    assert hook.pending_edit() == {}


def test_extras_stay_hidden_when_the_app_keeps_the_old_rows(env, monkeypatch):
    """The recycle task has not run 120 s after the rescan. The extras stay hidden, the entry stays, and
    the file goes on the list for a person."""
    mp4, mkv, extras, rows, recycled = radarr_extras(env, monkeypatch, late_task=10 ** 6)
    hook.main([])
    rec = decided(env)
    (key,) = hook.pending_edit()
    assert rec["repack"]["settle"]["waiting"] == [key] and rec["repack"]["settle"]["rows_left"] == 3 and recycled == []   # the .nfo row too
    assert sorted(os.listdir(os.path.join(os.path.dirname(mkv), hook.CFG.hide_dir))) == sorted(extras)
    (line,) = open(os.path.join(hook.CFG.state_dir, "convert-radarr.txt")).read().splitlines()
    assert line.split("\t")[1] == "extras_hidden" and "kept 3 extra rows of the old record 120 s after the rescan" in line


def test_extras_stay_hidden_while_the_app_still_lists_the_old_record(env, monkeypatch):
    """The first rescan did not drop the old record, so the extras stay hidden, and so does the pending entry in the
    store. The next --convert run settles it."""
    mp4, mkv, extras, rows, recycled = radarr_extras(env, monkeypatch)
    real = env["on_write"]
    env["on_write"] = lambda a, p, method, body: None if p == "command" and body["name"] == "RescanMovie" else real(a, p, method, body)
    hook.main([])
    rec = decided(env)
    (key,) = hook.pending_edit()
    assert rec["repack"]["settle"] == {"rescan": "completed", "waiting": [key]} and hook.pending_edit()[key]["state"] == "converted"
    assert hook.pending_edit()[key]["pid_ns"] == hook.pid_ns() == os.readlink("/proc/self/ns/pid")   # G5: the entry names its pid namespace
    assert sorted(os.listdir(os.path.join(os.path.dirname(mkv), hook.CFG.hide_dir))) == sorted(extras) and recycled == []
    env["on_write"] = real   # a later run: the entry's process is gone, so pending_recover() settles it
    hook.pending_edit(key, dict(hook.pending_edit()[key], pid=999999))
    monkeypatch.setattr(hook, "job_alive_pid", lambda pid, start=None: False)
    nfo = os.path.basename(mp4)[:-4] + ".nfo"
    assert hook.pending_recover("radarr", True) == {} and hook.pending_edit() == {} and recycled == [nfo]
    assert sorted(os.listdir(os.path.dirname(mkv))) == sorted([os.path.basename(mkv), nfo] + extras)


def test_a_stranded_conversion_is_reported_and_nothing_moves(env, monkeypatch, capsys):
    """A kill between the swap and the import leaves the original under its hidden name. pending_recover() prints it,
    logs it and moves nothing. It posts once per pending entry, so a later run posts nothing. A second conversion of
    the same file that strands in the same state posts again."""
    folder = os.path.dirname(env["path"])
    held = os.path.join(folder, ".x.avi" + hook.HELD.decode())
    open(held, "w").close()
    entry = dict(app="radarr", owner=7, path=os.path.join(folder, "x.avi"), new=os.path.join(folder, "x.mkv"), held=held, state="held",
                 pid=999999, extras=[])
    hook.pending_edit("radarr:1:ab", entry)
    monkeypatch.setattr(hook, "job_alive_pid", lambda pid, start=None: False)
    real, posted = hook.alert_findings, []   # the post has no decision line, so the test reads the findings it posts
    monkeypatch.setattr(hook, "alert_findings", lambda rec, size: posted.append(rec["findings"]) or real(rec, size))
    assert list(hook.pending_recover("radarr", True)) == ["radarr:1:ab"]
    assert "STRANDED" in capsys.readouterr().out and os.path.exists(held)
    (note,) = [r["note"] for r in log_lines(env) if r.get("result") == "warning"]
    assert note.startswith("a conversion to MKV stopped partway, after it hid the original.")
    assert posted == [[{"kind": "repack", "state": "held", "note": note}]]
    assert len([b for m, u, b in env["http"] if m == "POST"]) == 1
    assert list(hook.pending_recover("radarr", True)) == ["radarr:1:ab"] and len([b for m, u, b in env["http"] if m == "POST"]) == 1
    hook.pending_edit("radarr:1:ab")
    hook.pending_edit("radarr:2:cd", entry)
    assert list(hook.pending_recover("radarr", True)) == ["radarr:2:cd"] and len([b for m, u, b in env["http"] if m == "POST"]) == 2


@pytest.mark.parametrize("app_says, result", [("new", "completed"), ("nothing", "stranded")])
def test_an_undo_reads_the_app_before_it_deletes(env, monkeypatch, app_says, result):
    """The import went through, then a read of the app failed. The undo reads the app again: it
    lists the new file, so the conversion ends and the original goes. When the app does not answer at all, both files
    stay under their names, and the store keeps the pending entry for a person."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    real_lists = hook.app_lists
    monkeypatch.setattr(hook, "app_lists", lambda *a: (_ for _ in ()).throw(ConnectionError("Radarr restarts")))
    if app_says == "nothing":
        monkeypatch.setattr(hook, "app_now", lambda app, owner, items, tries=3: None)
    hook.main([])
    rec = decided(env)
    assert rec["repack"]["restored"]["result"] == result, rec
    if result == "completed":
        assert rec["outcome"] == "edited" and os.listdir(os.path.dirname(mkv)) == [os.path.basename(mkv)]
    else:
        assert rec["outcome"] == "repack_failed" and "both stay for a person" in rec["result"]
        assert sorted(os.listdir(os.path.dirname(mkv))) == sorted([os.path.basename(mp4), os.path.basename(mkv)])
        assert [e["state"] for e in hook.pending_edit().values()] == ["stranded"]
    del real_lists


def test_a_manual_import_that_times_out_is_cancelled(monkeypatch):
    """The command waits COMMAND_WAIT seconds at most. Then the hook asks the app to cancel it, so it never runs after
    the hook gave up on it."""
    writes, clock = [], [0.0]
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: writes.append((method, p)) or {"id": 42, "status": "queued"})
    monkeypatch.setattr(hook, "arr", lambda app, p: {"id": 42, "status": "queued"})
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hook.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert hook.ARR["sonarr"].command({"name": "ManualImport"}) == "timed out"
    assert writes == [("POST", "command"), ("DELETE", "command/42")]


def test_a_command_with_no_id_is_sent_and_never_read(monkeypatch):
    """The app answers a command with no id. The hook has nothing to wait on, so the import wait and the rescan wait both
    return "sent" at once."""
    reads, clock = [], [0.0]
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: {})
    monkeypatch.setattr(hook, "arr", lambda app, p: reads.append(p) or {})
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hook.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert (hook.ARR["radarr"].command({"name": "ManualImport"}), hook.ARR["sonarr"].rescan(5, 120), reads) == ("sent", "sent", [])


@pytest.mark.parametrize("state", hook.DONE)
def test_each_end_state_of_a_command_ends_the_wait(monkeypatch, state):
    """The import wait and the rescan wait share one set of end states. The first read that shows one of them ends the
    wait and returns it, and the hook never cancels the command."""
    writes, reads, clock = [], [], [0.0]
    monkeypatch.setattr(hook, "arr_write", lambda app, p, method, body=None: writes.append((method, p)) or {"id": 7, "status": "queued"})
    monkeypatch.setattr(hook, "arr", lambda app, p: reads.append(p) or {"id": 7, "status": state})
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hook.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert (hook.ARR["sonarr"].command({"name": "ManualImport"}), hook.ARR["radarr"].rescan(7, 120)) == (state, state)
    assert (writes, reads) == ([("POST", "command")] * 2, ["command/7"] * 2)


def test_an_import_converts_only_with_the_switch_on(env, monkeypatch, settings):
    """With CONVERT off, the MP4 import keeps its name and still gets
    its checks. A backfill converts with --convert whatever the switch says."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    settings(convert=False)
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "not_mkv" and "audio" in rec and env["repacks"] == [] and os.path.exists(mp4)
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "movieId": 7, "path": mp4})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--convert", "--apply"])
    assert os.path.exists(mkv) and not os.path.exists(mp4) and [r["outcome"] for r in log_lines(env) if r.get("outcome")][-1] == "repacked"


def test_a_taken_hidden_name_refuses_the_swap(env, monkeypatch):
    """A file under the original's hidden name would be replaced by the rename. The swap refuses before
    anything moves."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    os.mkdir(os.path.dirname(hook.held_name(mp4)))
    open(hook.held_name(mp4), "w").close()
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and "is taken" in rec["result"] and os.path.exists(mp4) and not os.path.exists(mkv)
    assert env["writes"] == []


@pytest.mark.parametrize("delay", [0, 4])
def test_radarr_names_its_file_by_movie_file_id(env, monkeypatch, delay):
    """movie/<id> fills movieFile from a join on the movie id, so while the original's record stays it shows the lowest
    file id, the original. movieFileId names the file Radarr assigned. A read of movieFile finds the new file not taken
    and rolls it back. The hook reads movieFileId, and reads again until it names the new file, here only after delay
    reads."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    records, state = {11: dict(env["movies"]["moviefile/11"])}, {"assigned": 11, "next": None, "reads": 0}
    real_arr, real_write = hook.arr, env["on_write"]

    def arr(app, p):
        if p == "movie/7":   # the join: movieFile is the first record of the movie, movieFileId the assigned one
            if state["next"] and state["reads"] >= delay:
                state["assigned"], state["next"] = state["next"], None
            state["reads"] += 1
            return dict(env["movies"]["movie/7"], movieFile=records[min(records)], movieFileId=state["assigned"])
        if p.startswith("moviefile/") and int(p.split("/")[1]) in records:
            return records[int(p.split("/")[1])]
        return real_arr(app, p)

    def app(a, p, method, body):
        real_write(a, p, method, body)
        if p == "command" and body["name"] == "ManualImport":
            fid = 12 if body["files"][0]["path"].endswith(".mkv") else 13
            records[fid] = {"id": fid, "path": body["files"][0]["path"], "customFormatScore": 25}
            state["next"], state["reads"] = fid, 0
    monkeypatch.setattr(hook, "arr", arr)
    env["on_write"] = app
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited" and rec["repack"]["relink"]["file_id"] == 12 and "restored" not in rec["repack"], rec["result"]
    assert os.listdir(os.path.dirname(mkv)) == [os.path.basename(mkv)]
    assert [w[2]["files"][0]["path"] for w in env["writes"] if w[1] == "command" and w[2]["name"] == "ManualImport"] == [mkv]


def test_movie_file_reads_the_record_movie_file_id_names(monkeypatch):
    """movie/<id> fills movieFile from a join on the movie id. movie_file() returns the record movieFileId names: from
    movieFile when the ids agree, else from moviefile/<id>. A movie with no assigned file has none. A shape with no
    movieFileId keeps movieFile. The subtitle hunter reads movies through it too."""
    reads = []
    monkeypatch.setattr(hook, "arr", lambda app, p: reads.append(p) or {"id": 12, "path": "/m/a.mkv"})
    old, new = {"id": 11, "path": "/m/a.mp4"}, {"id": 12, "path": "/m/a.mkv"}
    assert hook.movie_file({"movieFileId": 12, "movieFile": new}, "radarr") == new and reads == []
    assert hook.movie_file({"movieFileId": 12, "movieFile": old}, "radarr") == new and reads == ["moviefile/12"]
    assert hook.movie_file({"movieFileId": 0, "movieFile": old}, "radarr") == {} and hook.movie_file({"movieFile": old}, "radarr") == old
    import arr_subhunt
    assert "apps.movie_file(" in open(arr_subhunt.__file__).read() and '["movieFile"]' not in open(arr_subhunt.__file__).read()


def test_a_backfill_rescans_each_radarr_movie_right_after_its_conversion(env, monkeypatch):
    """Until the rescan, Radarr keeps the original's record, and movie/<id> shows it. So a
    backfill rescans each movie right after its conversion, not at the end of the run."""
    paths = mp4_films(env, monkeypatch, 2)
    hook.main(["--backfill", "radarr", "--convert", "--apply"])
    cmds = [(w[2]["name"], w[2].get("movieId") or w[2]["files"][0]["movieId"]) for w in env["writes"] if w[1] == "command"]
    assert cmds == [("ManualImport", 7), ("RescanMovie", 7), ("ManualImport", 8), ("RescanMovie", 8)], cmds
    assert all(os.path.exists(p[:-4] + ".mkv") for p in paths)


def test_a_lower_custom_format_score_puts_the_original_back(env, monkeypatch):
    """The app took the new file, but its custom format score fell from 25 to 5. The original goes back under its name,
    a second ManualImport links it again, and only then the new file goes. The file goes on the list."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    real = env["on_write"]

    def lower(app, p, method, body):
        real(app, p, method, body)
        if p == "moviefile/bulk":
            env["bulk"] = [dict(body[0], customFormatScore=5)]
    env["on_write"] = lower
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "repack_failed" and "the custom format score dropped from 25 to 5" in rec["result"], rec["result"]
    assert rec["repack"]["restored"]["result"] == "restored" and rec["repack"]["restored"]["listed"] == [mp4]
    assert [w[2]["files"][0]["path"] for w in env["writes"] if w[1] == "command" and w[2]["name"] == "ManualImport"] == [mkv, mp4]
    assert os.listdir(os.path.dirname(mp4)) == [os.path.basename(mp4)]
    (line,) = open(os.path.join(hook.CFG.state_dir, "convert-radarr.txt")).read().splitlines()
    assert line.split("\t")[1] == "repack_failed"


def test_a_backfill_converts_from_its_plan_with_a_canary_and_a_cap(env, monkeypatch, settings, tmp_path, capsys):
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    sent = refreshes(env, monkeypatch)
    other = tmp_path / "media" / "Other (2000)" / "Other (2000) SDTV.avi"
    other.parent.mkdir(); other.write_bytes(b"a" * 500)
    env["files"][str(other)] = copy.deepcopy(env["probe"])
    movie = dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "movieId": 7, "path": mp4})
    env["movies"]["movie"] = [movie, dict(movie, id=8, title="Other", year=2000, movieFile={"id": 81, "movieId": 8, "path": str(other)}),
                              dict(movie, id=9, title="Kept", movieFile={"id": 91, "movieId": 9, "path": "/m/Kept.mkv"})]
    env["movies"]["movie/8"] = dict(env["movies"]["movie/7"], title="Other", year=2000, path=str(other.parent), movieFile={"id": 81, "path": str(other)})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    plans = tmp_path / "plans.jsonl"
    hook.main(["--backfill", "radarr", "--convert", "--plan-out", str(plans)])
    rows = [json.loads(line) for line in plans.read_text().splitlines()]
    assert [(r["label"], r["class"], r["repack"][:13]) for r in rows] == [("Film A (1979)", "would repack mp4", "would repack:"),
                                                                        ("Other (2000)", "would repack avi", "would repack:")]
    assert "2 files that are not .mkv, dry run" in capsys.readouterr().out and env["repacks"] == [] and env["writes"] == []
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--plan-from", str(plans), "--ids", "8", "--canary", "1"])
    assert [r[-1] for r in env["repacks"]] == [str(other)]   # the canary takes its sample from the items --ids names
    assert os.path.exists(str(other)[:-4] + ".mkv") and not os.path.exists(other)
    assert [w[2]["name"] for w in env["writes"] if w[1] == "command"] == ["ManualImport", "RescanMovie"]   # the rescan once, after the run
    settings(convert_max=1)
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--plan-from", str(plans)])
    out = capsys.readouterr().out
    assert "stopped after 1 conversions, the cap of one run" in out and len(env["repacks"]) == 2 and os.path.exists(mkv)
    assert "rescan of movie 7: sent" in out
    assert [q["path"] for m, u, q in sent] == [[os.path.dirname(p)] for p in (str(other), mkv)] and analyzes(env) == []


@pytest.mark.parametrize("case", ["forced", "forced, not taken", "other refusal", "no refusal logged", "not in the work list"])
def test_a_person_can_force_a_conversion_the_proof_refuses(env, monkeypatch, settings, tmp_path, capsys, case):
    """A refused conversion keeps its original, and the decision log holds the refusal a person reviews. A run with
    --force-convert takes only the listed file. It converts the file when the proof refuses it with the same text. The
    decision line names the refusal in forced, the original stays in KEEP_DIR, so one move undoes it, and the nightly
    audit says forced. When the app does not take the new file, the original goes back and its kept link goes, and a
    second force converts it. A new refusal, or none in the log, is not forced, and the run says why. A path outside the
    work list is skipped."""
    first, second = mp4_films(env, monkeypatch, 2)
    mkv, refusal = first[:-4] + ".mkv", "stream video 0 (h264) holds 1006 packets in the new file, 1000 in the original"
    env["proof"] = ((0, refusal), [])
    root = tmp_path / hook.CFG.keep_dir
    monkeypatch.setattr(hook, "originals_root", lambda p: str(root))
    before = open(first, "rb").read()
    with pytest.raises(SystemExit):   # KEEP_ORIGINALS_DAYS 0 keeps no original
        hook.main(["--backfill", "radarr", "--convert", "--apply", "--force-convert", first])
    settings(keep_days=7)
    with pytest.raises(SystemExit):   # a dry run proves nothing
        hook.main(["--backfill", "radarr", "--convert", "--force-convert", first])
    if case != "no refusal logged":   # the refused run a person reviews, without the option
        hook.main(["--backfill", "radarr", "--convert", "--apply", "--ids", "7"])
        (rec,) = [r for r in log_lines(env) if r.get("outcome")]
        assert rec["outcome"] == "repack_failed" and rec["result"] == f"repack failed: {refusal}" and "forced" not in rec["repack"], rec
        assert os.path.exists(first) and not root.exists()
    if case == "other refusal":
        env["proof"] = ((1, "the packet data of stream audio 1 (aac) differ"), [])
    env["app_takes"] = case != "forced, not taken"
    capsys.readouterr(); env["repacks"].clear()
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--force-convert", "/m/Other.mp4" if case == "not in the work list" else first])
    out = capsys.readouterr().out
    assert os.path.exists(second) and {r[-1] for r in env["repacks"]} <= {first}, env["repacks"]   # only the listed file runs
    if case == "not in the work list":
        assert "not in this run's work list, skipped: /m/Other.mp4" in out and env["repacks"] == [], out
        return
    rec = [r for r in log_lines(env) if r.get("outcome")][-1]
    if case != "forced":
        assert rec["outcome"] == "repack_failed" and ("forced" in rec["repack"]) == (case == "forced, not taken") and "kept" not in rec["repack"], rec
        assert os.path.exists(first) and not os.path.exists(mkv) and os.stat(first).st_nlink == 1
        assert not [f for _, _, fs in os.walk(root) for f in fs]
        assert ("no proof refusal in the decision log, so it is not forced: " + first in out) == (case == "no refusal logged"), out
        assert "name check" not in out   # Radarr has no name check
        assert rec["repack"].get("not_forced") == ("the last refusal in the decision log differs: " + refusal if case == "other refusal" else None), rec
        if case != "forced, not taken":
            return
        env["app_takes"] = True   # the forced line keeps the refusal it overrode, so a second force converts
        hook.main(["--backfill", "radarr", "--convert", "--apply", "--force-convert", first])
        rec = [r for r in log_lines(env) if r.get("outcome")][-1]
    kept = rec["repack"]["kept"]
    assert rec["outcome"] == "repacked" and rec["repack"]["forced"] == refusal, rec
    assert sorted(os.listdir(os.path.dirname(first))) == sorted([os.path.basename(mkv), os.path.basename(second)])
    assert kept.startswith(str(root) + "/") and kept.endswith(os.path.basename(first)) and open(kept, "rb").read() == before
    assert os.stat(kept).st_nlink == 1   # the held name is gone
    assert hook.forced_note(rec["repack"]) == ", forced"


def test_backfill_conversions_run_side_by_side_and_swap_under_the_exclusive_lock(env, monkeypatch, tmp_path, capsys):
    """Two workers: both remuxes run at once under the shared file lock, a barrier proves it. Each swap and each flag
    edit holds the lock exclusive, and the app's import holds none. Every file gets its decision line."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    second = os.path.join(os.path.dirname(mp4), "Film A (1979) Extended.mp4")
    shutil.copy(mp4, second)
    env["files"][second] = copy.deepcopy(env["probe"])
    movie = dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "movieId": 7, "path": mp4})
    env["movies"]["movie"] = [movie, dict(movie, id=8, title="Film A Extended", movieFile={"id": 81, "movieId": 8, "path": second})]
    env["movies"]["movie/8"] = dict(env["movies"]["movie/7"], movieFile={"id": 81, "path": second})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    both = threading.Barrier(2, timeout=10)
    env["during_repack"] = both.wait   # a remux waits here until the other remux runs too
    held, locks = {}, []   # the lock state of each thread: the flock ops on the file lock, and what held during the import

    def flock(f, op):
        if f.name.endswith("/lock"):
            held[threading.get_ident()] = op
        REAL_FLOCK(f, op)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    real = env["on_write"]
    env["on_write"] = lambda app, p, method, body: locks.append((p, held.get(threading.get_ident()))) or real(app, p, method, body)
    real_run = hook.subprocess.run

    def run(argv, **kw):
        if argv[0] == "mkvpropedit":
            locks.append(("mkvpropedit", held.get(threading.get_ident())))
        if "mkvmerge" in argv:
            locks.append(("remux", held.get(threading.get_ident())))
        return real_run(argv, **kw)
    monkeypatch.setattr(hook.subprocess, "run", run)
    real_link = os.link
    monkeypatch.setattr(hook.os, "link", lambda a, b: locks.append(("swap", held.get(threading.get_ident()))) or real_link(a, b))
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--workers", "2"])
    assert sorted(os.listdir(os.path.dirname(mkv))) == sorted([os.path.basename(mkv), "Film A (1979) Extended.mkv"])
    out = capsys.readouterr().out
    assert "2 files that are not .mkv, APPLY" in out and 'summary: {"repacked": 2}' in out   # each file counts once
    assert sorted(x for x in out.splitlines() if x.startswith("repacked")) == ["repacked     Film A (1979)", "repacked     Film A Extended (1979)"]
    assert [r["outcome"] for r in log_lines(env) if "schema" in r] == ["repacked", "repacked"]
    # swap_lock() takes the exclusive lock without the gate, and no flag edit follows in a --convert run
    assert set(locks) == {("remux", fcntl.LOCK_SH), ("swap", fcntl.LOCK_EX | fcntl.LOCK_NB), ("command", fcntl.LOCK_UN), ("moviefile/bulk", fcntl.LOCK_UN),
                          ("command", None)}, locks   # the rescans at the end of the run, in the main thread, hold no lock either


def mp4_films(env, monkeypatch, n):
    """n MP4 files of n Radarr movies in one folder, Film A (1979) as movie 7 and copies as movies 8 and up. Returns
    their paths."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    paths, movies = [mp4], [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "movieId": 7, "path": mp4})]
    for k in range(1, n):
        p = os.path.join(os.path.dirname(mp4), f"Film A (1979) Cut {k}.mp4")
        shutil.copy(mp4, p)
        env["files"][p] = copy.deepcopy(env["probe"])
        movies.append(dict(movies[0], id=7 + k, title=f"Film A {k}", movieFile={"id": 80 + k, "movieId": 7 + k, "path": p}))
        env["movies"][f"movie/{7 + k}"] = dict(env["movies"]["movie/7"], movieFile={"id": 80 + k, "path": p})
        paths.append(p)
    env["movies"]["movie"] = movies
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    return paths


def test_a_conversion_worker_whose_original_changed_during_the_remux_drops_its_temp_file(env, monkeypatch):
    """Two workers remux side by side. The app imports an upgrade of the first file meanwhile. Its swap takes the lock
    exclusive, finds another inode, size and mtime, drops the temp file and lists the file. The upgrade stays, and the
    second file converts."""
    first, second = mp4_films(env, monkeypatch, 2)
    both, once = threading.Barrier(2, timeout=10), threading.Lock()

    def upgrade():   # both remuxes run, then the app replaces the first original
        both.wait()
        if once.acquire(blocking=False):
            with open(first + ".part", "wb") as f:
                f.write(b"u" * 2000)
            os.replace(first + ".part", first)
    env["during_repack"] = upgrade
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--workers", "2"])
    recs = {r["path"]: r for r in log_lines(env) if r.get("outcome")}
    assert recs[first]["outcome"] == "repack_source_changed" and "the app replaced or renamed the original" in recs[first]["result"], recs
    assert recs[second[:-4] + ".mkv"]["outcome"] == "repacked"
    assert sorted(os.listdir(os.path.dirname(first))) == sorted([os.path.basename(first), os.path.basename(second)[:-4] + ".mkv"])
    assert open(first, "rb").read() == b"u" * 2000   # no temp file, no .mkv and no held name of the first file
    assert [w[2]["files"][0]["path"] for w in env["writes"] if w[1] == "command" and w[2]["name"] == "ManualImport"] == [second[:-4] + ".mkv"]
    (line,) = open(os.path.join(hook.CFG.state_dir, "convert-radarr.txt")).read().splitlines()
    assert line.split("\t")[1] == "repack_source_changed" and line.endswith(first)


def test_a_conversion_worker_lets_its_slot_go_while_the_app_imports(env, monkeypatch):
    """Two slots, three files. Each ManualImport waits until all three remuxes ran. The third remux can start only when
    a worker that waits for the app's import holds no slot."""
    paths = mp4_films(env, monkeypatch, 3)
    real, waits = env["on_write"], []

    def slow_import(app, p, method, body):
        if p == "command" and body["name"] == "ManualImport":
            for _ in range(1000):   # 10 s of real time. The env fixture fakes time.sleep and the clock.
                if len(env["repacks"]) >= 3:
                    break
                threading.Event().wait(0.01)
            waits.append(len(env["repacks"]))
        return real(app, p, method, body)
    env["on_write"] = slow_import
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--workers", "2"])
    assert waits == [3, 3, 3] and [r["outcome"] for r in log_lines(env) if "schema" in r] == ["repacked"] * 3
    assert sorted(os.listdir(os.path.dirname(paths[0]))) == sorted(os.path.basename(p)[:-4] + ".mkv" for p in paths)


def test_a_convert_backfill_skips_the_hearing_and_the_flag_work(env, monkeypatch):
    """A --convert run only converts. An AVI often has an untagged audio track, and its hearing
    takes time, one model at a time, with a worker slot held. The language backfill decides the flags
    later, from its own cache. The rescan still goes out once per item at the end of the run."""
    paths = mp4_films(env, monkeypatch, 2)
    real_retag = hook.arr_decide.retag
    monkeypatch.setattr(hook.arr_decide, "retag", lambda j, *a, **k: dict(real_retag(j, *a, **k), ask={"a1"}))
    monkeypatch.setattr(hook, "hear", lambda *a, **k: pytest.fail("a --convert run hears nothing"))
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--workers", "2"])
    assert sorted(r["outcome"] for r in log_lines(env) if "schema" in r) == ["repacked", "repacked"] and env["mkvpropedit"] == []
    assert sorted(w[2]["movieId"] for w in env["writes"] if w[1] == "command" and w[2]["name"] == "RescanMovie") == [7, 8]
    assert all(os.path.exists(p[:-4] + ".mkv") for p in paths)


def test_a_convert_dry_run_reads_side_by_side_in_file_order(env, monkeypatch, capsys):
    """A --convert dry run takes every file that is not .mkv, with no selection by tracks, and SCAN_WORKERS or --workers
    read them side by side under the shared lock, as a dry run of the flags does. Nothing is written."""
    paths = mp4_films(env, monkeypatch, 3)
    monkeypatch.setattr(hook, "selected", lambda p: False)   # never asked for a --convert run
    hook.main(["--backfill", "radarr", "--convert", "--workers", "2"])
    assert "3 files that are not .mkv, dry run, 2 at a time" in capsys.readouterr().out
    assert [(r["path"], r["outcome"]) for r in log_lines(env) if r.get("outcome")] == [(p, "would_repack") for p in paths]
    assert env["repacks"] == [] and env["writes"] == [] and all(os.path.exists(p) for p in paths)


def test_the_imports_of_one_series_that_wait_go_out_in_one_command(monkeypatch):
    """The first file of a series sends its ManualImport at once. Two more arrive while it runs and go out together in
    the next command. A command that raises answers every file it carried, so no worker waits forever."""
    sent, first = [], threading.Event()

    def command(self, body):
        sent.append([f["path"] for f in body["files"]])
        if len(sent) == 1:
            first.wait(10)
            return "completed"
        raise RuntimeError("Sonarr is down")
    monkeypatch.setattr(hook.Sonarr, "command", command)
    imports, out = hook.Imports(), {}
    threads = {p: threading.Thread(target=lambda p=p: out.__setitem__(p, imports.run("sonarr", 5, {"path": p}))) for p in "abc"}
    threads["a"].start()
    while not sent:
        threading.Event().wait(0.01)
    threads["b"].start(); threads["c"].start()
    while len(imports.waiting[("sonarr", 5)]) < 3:
        threading.Event().wait(0.01)
    first.set()
    for t in threads.values():
        t.join(10)
    assert sent == [["a"], ["b", "c"]] and imports.waiting == {}
    assert out == {"a": "completed", "b": "error: RuntimeError: Sonarr is down", "c": "error: RuntimeError: Sonarr is down"}


def test_a_backfill_scans_a_folder_once_for_all_its_renamed_files(env, monkeypatch, tmp_path):
    """The episodes of a season share a folder. A backfill run scans it once, after the last of them is in place, and
    rescans each item once."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    sent = refreshes(env, monkeypatch)
    second = os.path.join(os.path.dirname(mp4), "Film A (1979) Extended.mp4")
    shutil.copy(mp4, second)
    env["files"][second] = copy.deepcopy(env["probe"])
    movie = dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "movieId": 7, "path": mp4})
    env["movies"]["movie"] = [movie, dict(movie, id=8, title="Film A Extended", movieFile={"id": 81, "movieId": 8, "path": second})]
    env["movies"]["movie/8"] = dict(env["movies"]["movie/7"], movieFile={"id": 81, "path": second})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--convert", "--apply"])
    assert sorted(os.listdir(os.path.dirname(mkv))) == sorted([os.path.basename(mkv), "Film A (1979) Extended.mkv"])
    assert [q["path"] for m, u, q in sent] == [[os.path.dirname(mkv)]] and analyzes(env) == []
    assert [r["plex_reason"] for r in log_lines(env) if r.get("result") == "plex"] == ["plex_scan_sent"]
    assert [w[2] for w in env["writes"] if w[1] == "command" and w[2]["name"] == "RescanMovie"] == [{"name": "RescanMovie", "movieId": m} for m in (7, 8)]


def test_a_plex_later_run_records_its_folders_and_scans_none(env, monkeypatch, tmp_path):
    """--plex-later: a --convert --apply run sends no folder scan. It lists each folder it touched in the store, at once,
    for --plex-flush."""
    paths = mp4_films(env, monkeypatch, 2)
    sent = refreshes(env, monkeypatch)
    hook.main(["--backfill", "radarr", "--convert", "--apply", "--plex-later"])
    assert all(os.path.exists(p[:-4] + ".mkv") for p in paths) and sent == [] and analyzes(env) == []
    listed = hook.store.get("plex-later", "radarr")
    assert set(listed) == {os.path.dirname(paths[0])} and len(listed) == 2
    assert not [r for r in log_lines(env) if r.get("result") == "plex"]
    with pytest.raises(SystemExit):   # it only goes with --convert --apply
        hook.main(["--backfill", "radarr", "--convert", "--plex-later"])


def log_at_clock(rec):
    """A decision line stamped with the fake clock, as log() stamps it with the real one, and its facts in the store."""
    now = datetime.datetime.fromtimestamp(hook.time.time()).astimezone()
    with open(hook.CFG.log, "a") as f:
        f.write(json.dumps(dict(time=now.isoformat(timespec="seconds"), **rec)) + "\n")
    hook.log_facts(rec, now.replace(microsecond=0).timestamp())


def test_plex_flush_sends_one_gated_scan_per_section(env, monkeypatch, tmp_path):
    """--plex-flush: the listed folders of one library location get one partial scan at its root. It waits
    PLEX_SCAN_AFTER after the section's last analyze in the decision log, then the two idle checks, a busy section
    included. A folder no section holds stays on the list."""
    sent = refreshes(env, monkeypatch)
    root = str(tmp_path / "media")
    hook.store.put("plex-later", "sonarr", [f"{root}/Show A/Season 1", f"{root}/Show B/Season 2", f"{root}/Show A/Season 1", "/elsewhere/Show C"])
    log_at_clock(dict(app="sonarr", source="hook", result="plex", plex="analyze sent for 1", plex_reason="plex_analyze_sent", section="12"))
    start = hook.time.time()
    env["activities"] = [[{"type": "library.update.section", "Context": {"librarySectionID": "12"}}]]   # busy once
    hook.main(["--plex-flush", "sonarr"])
    assert [(u, q["path"]) for m, u, q in sent] == [("/library/sections/12/refresh", [root])]
    assert hook.time.time() - start >= hook.PLEX_SCAN_AFTER and len(env["checks"]) >= 3   # the wait, then busy, idle, idle
    (line,) = [r for r in log_lines(env) if r.get("path") == root and r["result"] == "plex"]
    assert (line["plex_reason"], line["folders"], line["section"]) == ("plex_scan_sent", 2, "12")
    assert hook.store.get("plex-later", "sonarr") == ["/elsewhere/Show C"]


@pytest.mark.parametrize("own", [False, True])
def test_plex_flush_maps_the_listed_folders_to_the_plex_paths(env, monkeypatch, settings, tmp_path, own):
    """The list holds local paths. The folder job holds a local path too, so the scan maps it to Plex's path once. The
    second pair makes the Plex root a local prefix as well, which a second mapping would turn wrong. Plex takes
    PLEX_PATH_MAP, else PATH_MAP. A Sonarr map that differs never reaches Plex."""
    sent = refreshes(env, monkeypatch)
    root = str(tmp_path / "media")   # where Plex sees the files
    pairs = [(root, "/local/media"), ("/elsewhere", root)]
    settings(path_map=[] if own else pairs, plex_path_map=pairs if own else [], sonarr={"path_map": [("/tv", "/local/media")] if own else []})
    hook.store.put("plex-later", "sonarr", ["/local/media/Show A/Season 1"])
    hook.main(["--plex-flush", "sonarr"])
    assert [(u, q["path"]) for m, u, q in sent] == [("/library/sections/12/refresh", [root])]
    assert hook.store.get("plex-later", "sonarr") == []


@pytest.mark.parametrize("shape", ["hook", "backfill"])
def test_plex_flush_waits_again_for_an_analyze_during_its_wait(env, monkeypatch, tmp_path, shape):
    """A live import's analyze can land while the flush waits, and a scan 15 to 30 s after it is
    the race that can crash Plex. The flush reads the decision log again right before the send, so it waits again and
    takes two new idle checks. An analyze counts in both shapes the log has: the hook worker's plex line and a flag
    backfill's decision line with plex_section."""
    sent = refreshes(env, monkeypatch)
    root = str(tmp_path / "media")
    hook.store.put("plex-later", "sonarr", [f"{root}/Show A/Season 1"])
    analyze = (dict(app="sonarr", source="hook", result="plex", plex="analyze sent", plex_reason="plex_analyze_sent", section="12")
               if shape == "hook" else
               dict(app="sonarr", source="backfill", result="edited", plex="analyze sent", plex_reason="plex_analyze_sent", plex_section="12"))
    real_sleep, landed, at = hook.time.sleep, [], []

    def sleep(sec):   # an import's analyze lands during the flush's first long wait
        real_sleep(sec)
        if sec >= 100 and not landed:
            landed.append(hook.time.time())
            log_at_clock(analyze)
    monkeypatch.setattr(hook.time, "sleep", sleep)
    real_http = hook.http
    monkeypatch.setattr(hook, "http", lambda url, *a, **k: (at.append(hook.time.time()) if "/refresh" in url else None) or real_http(url, *a, **k))
    log_at_clock(analyze)   # one analyze before the flush starts
    hook.main(["--plex-flush", "sonarr"])
    assert [(u, q["path"]) for m, u, q in sent] == [("/library/sections/12/refresh", [root])] and landed
    assert at[0] - landed[0] >= hook.PLEX_SCAN_AFTER - 5, (at, landed)   # the scan waited again after the analyze in the wait
    assert [r["plex_reason"] for r in log_lines(env) if r.get("result") == "plex_deferred"][:1] == ["plex_scan_after_analyze"]


def test_a_stopped_flush_keeps_the_unscanned_folders_listed(env, monkeypatch, tmp_path):
    """A stop during a wait of the flush must not lose the list. The folders of a section leave
    it only once its scan went out. SIGTERM in the wait of the second section leaves its folders listed, and the one no
    section holds."""
    a, b = str(tmp_path / "media" / "tv"), str(tmp_path / "media" / "tv2")
    monkeypatch.setattr(hook, "plex_get", lambda path, **k: {"Directory": [{"key": "12", "Location": [{"path": a}]},
                                                                          {"key": "13", "Location": [{"path": b}]}]})
    sent = refreshes(env, monkeypatch)
    hook.store.put("plex-later", "sonarr", [f"{a}/Show A/Season 1", f"{b}/Show B/Season 2", "/elsewhere/Show C"])
    real_sleep = hook.time.sleep

    def sleep(sec):   # SIGTERM once the first section's scan went out: the default action ends the process
        real_sleep(sec)
        if sent:
            raise SystemExit(128 + signal.SIGTERM)
    monkeypatch.setattr(hook.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        hook.main(["--plex-flush", "sonarr"])
    assert [q["path"] for m, u, q in sent] == [[a]]
    assert hook.store.get("plex-later", "sonarr") == [f"{b}/Show B/Season 2", "/elsewhere/Show C"]


# --- the nightly audit post -------------------------------------------------------------------

def audit_line(label, n, rules, tmdb="ok", **kw):
    """A hook decision line as the audit reads it: an edit of episode n of label with rules, as (selector, new, rule)."""
    before = [{"pos": "a1", "sel": "track:=1", "lang": "spa", "default": 1, "role": "main"},
              {"pos": "a2", "sel": "track:=2", "lang": "eng", "default": 0, "role": "main"},
              {"pos": "s1", "sel": "track:=3", "lang": "eng", "default": 0, "role": "full"}]
    return dict(time=datetime_now(), app="sonarr", source="hook", apply=True, outcome="edited", result="edited", label=f"{label} S01E{n:02d}",
                path=f"/tv/{label}/{n}.mkv", before=before, edits=[[sel, new, 1 - new] for sel, new, _ in rules], edit_rules=[r for _, _, r in rules],
                recheck={"edits": 0, "invariants": []}, tmdb=tmdb, **{"class": "x", **kw})


def datetime_now():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def post_audit(env, lines):
    """The audit of the decision lines lines, kept in the store as logs.decision() keeps them."""
    for r in lines:
        hook.store.decided(datetime.datetime.fromisoformat(r["time"]).timestamp(), r["app"], r["path"],
                           json.dumps({k: r[k] for k in hook.KEPT_KEYS if k in r}))
    hook.main(["--audit", "sonarr", "--since", "24h", "--post"])
    (post,) = [b for m, u, b in env["http"] if m == "POST"]
    return post


ENGLISH_FIRST = [("track:=2", 1, "audio switched"), ("track:=1", 0, "audio switched"), ("track:=3", 0, "foreign subtitle off")]


def test_a_clean_day_posts_no_audit(env):
    """A day of clean changes posts nothing. The summary line still goes to syslog."""
    lines = [audit_line("Show H", n, ENGLISH_FIRST) for n in (1, 2, 3)] + [audit_line("Show I", 1, [("track:=3", 0, "forced flag cleared")])]
    for r in lines:
        hook.store.decided(datetime.datetime.fromisoformat(r["time"]).timestamp(), r["app"], r["path"],
                           json.dumps({k: r[k] for k in hook.KEPT_KEYS if k in r}))
    hook.main(["--audit", "sonarr", "--since", "24h", "--post"])
    assert [b for m, u, b in env["http"] if m == "POST"] == []
    assert env["syslog"][-1].startswith("arr=sonarr source=audit outcome=summary edited=4 further=0 undecided=0"), env["syslog"]


def test_the_audit_lists_every_file_with_the_problems_first(env):
    """A day with problems posts one line per file: its problems in plain words, then the files that are OK. The lines
    past the limit end in "and N more OK". TMDB shows in the footer only when it had trouble."""
    lines = [audit_line(f"Show {k:03d}", 1, ENGLISH_FIRST) for k in range(120)]
    lines += [dict(audit_line("Show J", 1, []), outcome="undecided", result="undecided: the app says Japanese", abstain="original_missing_bare_tag"),
              dict(audit_line("Show 000", 1, ENGLISH_FIRST), tmdb="tmdb_unavailable", recheck={"edits": 1, "invariants": []}),
              dict(audit_line("Show K", 1, ENGLISH_FIRST), recheck={"edits": 0, "invariants": ["inv_only_english_subtitle_off"]})]
    post = post_audit(env, lines)
    e, host = post["embeds"][0], hook.CFG.instance
    body = e["description"].split("\n")
    assert (e["title"], e["color"], e["fields"]) == (f"Audit check: 3 problems · Sonarr {host}", hook.COLORS["amber"], [])
    assert body[:4] == ["**Show 000 S01E01**: a check after the change still finds tracks to change",
                        "**Show J S01E01**: couldn't decide which audio should play first, because no track is in the original language",
                        "**Show K S01E01**: after the change, the only English subtitles are off", "**Show 001 S01E01**: OK"]
    assert body[-1] == f"and {122 - (len(body) - 1)} more OK" and len(e["description"]) <= 2000, body[-3:]
    assert e["footer"]["text"] == f"TMDB didn't answer 1 time · arr-media-guard on {host}"
    assert "invariant" not in e["description"] and "undecided" not in e["description"]


@pytest.fixture(scope="module")
def convertible(tmp_path_factory):
    """A file of each container the conversion takes, made with ffmpeg: an MP4 with timed text and three
    sidecars (one in cp1252), a transport stream, an AVI with MPEG-4 part 2 and MP3, and an ASF that mkvmerge cannot read."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d = tmp_path_factory.mktemp("convert")
    src = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=20", "-f", "lavfi", "-i", "sine=duration=20"]
    (d / "s.srt").write_text("1\n00:00:01,000 --> 00:00:03,500\nHello <i>there</i>\n\n2\n00:00:05,000 --> 00:00:07,000\nTwo\nlines\n")
    subprocess.run(src + ["-i", str(d / "s.srt"), "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "ultrafast", "-bf", "2",
                          "-c:a", "aac", "-c:s", "mov_text", str(d / "Movie (2020).mp4")], check=True)
    (d / "Movie (2020).en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\n<i>World</i>\n")
    (d / "Movie (2020).en.forced.srt").write_text("1\n00:00:04,000 --> 00:00:05,000\n[SIGN]\n")
    (d / "Movie (2020).es.srt").write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nCaf\xe9\n")
    subprocess.run(src + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(d / "Clip.ts")], check=True)
    subprocess.run(src + ["-c:v", "mpeg4", "-bf", "2", "-vtag", "DX50", "-c:a", "libmp3lame", str(d / "Clip.avi")], check=True)
    subprocess.run(src + ["-c:v", "wmv2", "-c:a", "wmav2", str(d / "Clip.wmv")], check=True)
    style = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontsize\nStyle: Default,20\n\n[Events]\n"
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    (d / "lead.ass").write_text(style + "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,It's a first line.\n"
                                "Dialogue: 0,0:00:03.00,0:00:05.00,Default,,0,0,0,,\\NA second line after a break,\n")
    subprocess.run(src[:8] + ["-i", str(d / "lead.ass"), "-map", "0", "-map", "1", "-c:v", "libx264", "-preset", "ultrafast", "-c:s", "mov_text",
                              str(d / "Lead.mp4")], check=True)
    return d


AUD = "filter_units=remove_types=9"   # H.264 access unit delimiters, which mkvmerge drops


def remux(path, out):
    """convert_cmd() run for real, as convert() runs it, with the sidecars and the ffprobe streams it would pass."""
    j = hook.mkvmerge(str(path))
    unreadable = (j.get("container") or {}).get("supported") is False
    subs = [] if unreadable else hook.sidecar_subs(str(path))
    r = subprocess.run(hook.convert_cmd(str(path), str(out), j, subs, hook.ff_streams(str(path))[1] if unreadable else None)[5:],
                       capture_output=True, text=True)
    assert r.returncode == 0 and not (r.stdout + r.stderr).strip(), r.stdout + r.stderr
    return subs


def proved(*a, **k):
    """prove() with the text of its refusal only, see test_a_refusal_names_the_stream_of_its_packets()."""
    refused, proof = hook.prove(*a, **k)
    return refused and refused[1], proof


@pytest.mark.parametrize("name, methods", [
    ("Movie (2020).mp4", [f"packets, original through {AUD}, new file through {AUD}", "packets", "text", "sidecar text", "sidecar text",
                          "sidecar text"]),
    ("Clip.ts", [f"packets, original through {AUD}, new file through h264_mp4toannexb,{AUD}", "packets, original through aac_adtstoasc"]),
    ("Clip.avi", ["packets", "packets"]),
    ("Clip.wmv", ["packets", "packets"]),
])
def test_a_real_remux_proves_every_stream_at_packet_level(convertible, tmp_path, name, methods):
    out = tmp_path / "new.mkv"
    subs = remux(convertible / name, out)
    fault, proof = proved(str(convertible / name), str(out), subs, str(tmp_path))
    assert fault is None and [p["method"] for p in proof] == methods and all(p["match"] for p in proof), (fault, proof)
    if subs:
        assert [(s["name"], s["lang"], s["flags"], s["charset"]) for s in subs] == [
            ("Movie (2020).en.forced.srt", "en", ["--forced-display-flag"], "UTF-8"), ("Movie (2020).en.srt", "en", [], "UTF-8"),
            ("Movie (2020).es.srt", "es", [], "cp1252")]
        tracks = [(t["properties"]["language"], t["properties"].get("forced_track")) for t in hook.mkvmerge(str(out))["tracks"][3:]]
        assert tracks == [("eng", True), ("eng", False), ("spa", False)]


def test_a_wmv_that_starts_before_its_first_keyframe_converts_whole(tmp_path):
    """ASF and WMV go through ffmpeg -c copy. With -copyinkf the new file keeps the frames before the first keyframe and
    proves clean. The same remux without it loses them, and the proof refuses it."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    full, src, new, lost = (tmp_path / n for n in ("full.wmv", "Clip.wmv", "new.mkv", "lost.mkv"))
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=6", "-f", "lavfi", "-i", "sine=duration=6",
              "-c:v", "wmv2", "-g", "48", "-c:a", "wmav2", str(full)], check=True)
    REAL_RUN(["ffmpeg", "-v", "quiet", "-y", "-i", str(full), "-ss", "1.1", "-map", "0", "-c", "copy", "-copyinkf", str(src)], check=True)
    remux(src, new)   # convert_cmd(), as convert() runs it for a container mkvmerge cannot read
    fault, proof = proved(str(src), str(new), [], str(tmp_path))
    flags = REAL_RUN(["ffprobe", "-v", "error", "-select_streams", "0", "-show_entries", "packet=flags", "-of", "csv=p=0", str(src)],
                     capture_output=True, text=True).stdout.split()
    assert fault is None and "K" not in flags[0] and proof[0]["count"] == len(flags), (fault, proof)
    argv = hook.convert_cmd(str(src), str(lost), hook.mkvmerge(str(src)), [], hook.ff_streams(str(src))[1])[5:]
    REAL_RUN([a for a in argv if a != "-copyinkf"], check=True)
    fault, _ = proved(str(src), str(lost), [], str(tmp_path))
    assert fault and fault.startswith("stream video 0 (wmv2) holds "), fault


def test_frames_before_the_first_keyframe_are_proven(tmp_path):
    """An MP4 can start with frames before its first keyframe. A plain -c copy drops them, so a proof that read both
    files that way passed a new file that lost them. With -copyinkf the proof reads them: mkvmerge keeps them and
    passes, and an ffmpeg copy that lost them fails. The one-packet read also starts at packet 0."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    full, src, kept, lost = (tmp_path / n for n in ("full.mp4", "Clip.mp4", "kept.mkv", "lost.mkv"))
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=6", "-f", "lavfi", "-i", "sine=duration=6",
              "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-bf", "2", "-c:a", "aac", str(full)], check=True)
    REAL_RUN(["ffmpeg", "-v", "quiet", "-y", "-i", str(full), "-ss", "1.1", "-c", "copy", "-copyinkf", str(src)], check=True)   # a cut mid-GOP
    remux(src, kept)
    REAL_RUN(["ffmpeg", "-v", "quiet", "-y", "-i", str(src), "-c", "copy", str(lost)], check=True)
    fault, proof = proved(str(src), str(kept), [], str(tmp_path))
    flags = REAL_RUN(["ffprobe", "-v", "error", "-select_streams", "0", "-show_entries", "packet=flags,size", "-of", "csv=p=0", str(src)],
                     capture_output=True, text=True).stdout.split()
    before = next(i for i, f in enumerate(flags) if "K" in f.split(",")[1])
    assert fault is None and before > 10 and proof[0]["count"] == len(flags), (fault, proof, before)
    assert len(hook.packet_data(str(src), 0, None, 1, 60)) == int(flags[0].split(",")[0])   # packet 0, no keyframe
    fault, _ = proved(str(src), str(lost), [], str(tmp_path))
    assert fault == f"stream video 0 (h264) holds {len(flags) - before} packets in the new file, {len(flags)} in the original", fault


@pytest.mark.parametrize("tamper, why", [
    (["-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-c:s", "srt"], "the packet data of stream audio 1 (aac) differ"),
    (["-c:v", "copy", "-c:a", "copy", "-c:s", "srt", "-t", "10"], "stream video 0 (h264) holds 242 packets in the new file, 480 in the original"),
    (["-c:v", "copy", "-c:a", "copy", "-sn"], "the new file holds 0 subtitle streams, not 1"),
])
def test_the_proof_refuses_a_copy_that_is_not_lossless(convertible, tmp_path, tamper, why):
    """No stream is decoded: a re-encoded stream, a short copy or a lost stream shows in the packets or the stream count."""
    out = tmp_path / "new.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(convertible / "Movie (2020).mp4"), *tamper, "-f", "matroska", str(out)], check=True)
    fault, _ = proved(str(convertible / "Movie (2020).mp4"), str(out), [], str(tmp_path))
    assert fault == why, fault


def test_the_text_compare_reads_timed_text_as_ffmpeg_writes_it():
    """ffmpeg writes MP4 timed text with tags, and a run of spaces as ASS hard spaces, on one side only. The text is
    the same. A last cue may last 134 s in the MP4, to the end of the track, and 1.5 s after mkvmerge. Only the last
    cue's end may differ."""
    src = ('1\n00:00:41,207 --> 00:00:44,915\n<font size="24"><b><i>It\'s a first line\nand its second half</i></b>'
           '</font><font size="24">.</font>\n\n2\n00:04:16,000 --> 00:04:18,000\nHi. \\h\\h \\hWhere does this line go?\n')
    new = ("1\n00:00:41,207 --> 00:00:44,915\nIt's a first line\nand its second half.\n\n"
           "2\n00:04:16,000 --> 00:04:18,000\nHi. \\h\\h\\hWhere does this line go?\n")
    assert hook.cue_fault("the text", hook.srt_cues(src), hook.srt_cues(new)) is None
    last_cue = "1\n00:47:11,000 --> 00:47:15,000\nA line before the last\n\n2\n00:47:15,360 --> 00:49:29,580\n\u00b6 THE LAST LINE \u00b6\n"
    short = last_cue.replace("00:49:29,580", "00:47:16,860")
    assert hook.cue_fault("the mov_text stream 3", hook.srt_cues(last_cue), hook.srt_cues(short), last_end=False) is None
    assert hook.cue_fault("the mov_text stream 3", hook.srt_cues(last_cue), hook.srt_cues(short)) == \
        "the mov_text stream 3 times the cue '\u00b6 THE LAST LINE \u00b6' 2835.360 to 2836.860 s in the new file, 2835.360 to 2969.580 s in the original"
    moved = short.replace("00:47:15,000", "00:47:15,300")   # any other cue keeps its end
    assert hook.cue_fault("t", hook.srt_cues(last_cue), hook.srt_cues(moved), last_end=False).startswith("t times the cue 'A line before the last'")


@pytest.mark.parametrize("stderr, fails", [
    ("[mp3float @ 0x5a5a00003000] Header missing\n", False),   # the decoder of ffmpeg's stream probe
    ("[h264 @ 0x5a5a00004000] non-existing PPS 0 referenced\n    Last message repeated 1 times\n", False),   # a decoder message too
    ("[mpegts @ 0x55f0] Packet corrupt (stream = 0, dts = 1)\n", True),   # the demuxer: a failed read
    ("[filter_units @ 0x55f0] Failed to read unit 0 (type 5).\n", True),   # a proof filter
    ("Error while filtering: Invalid data found when processing input\n", True),
])
def test_only_a_decoder_message_of_the_stream_probe_passes_the_read(monkeypatch, tmp_path, stderr, fails):
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: open(argv[-1], "w").close() or types.SimpleNamespace(returncode=0, stderr=stderr))
    if fails:
        with pytest.raises(RuntimeError, match="did not read x.avi cleanly"):
            hook.packet_hashes("/m/x.avi", [0], {}, [], str(tmp_path), 60)
    else:
        assert hook.packet_hashes("/m/x.avi", [0], {}, [], str(tmp_path), 60) == ({}, {})


@pytest.mark.parametrize("lost, fault", [
    (1, None),   # only the cut last frame, which passes
    (2, "stream audio 1 (ac3) holds 5065 packets in the new file, 5067 in the original"),   # two packets lost
])
def test_a_cut_last_frame_passes_and_is_logged(monkeypatch, tmp_path, lost, fault):
    """In an AVI, the original's last AC3 packet is a cut frame of 213 bytes, and mkvmerge
    drops it. Every other one of the 5,067 packets matches. The proof passes and names the frame. Any other lost packet
    still fails."""
    streams = [{"index": 0, "codec_type": "video", "codec_name": "mpeg4"}, {"index": 1, "codec_type": "audio", "codec_name": "ac3"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("avi" if p.endswith(".avi") else "matroska,webm", streams, 391.0))
    video = {"count": 9765, "empty": 0, "digest": "v", "but_last": "v9", "last": 2000, "last_pts": 390.9, "start": 0.0, "end": 391.0}

    def hashes(path, maps, bsf, texts, folder, timeout, raw=False):
        audio = {"count": 5067, "empty": 0, "digest": "a5067", "but_last": "a5066", "last": 213, "last_pts": 390.96, "start": 0.0, "end": 391.0} \
            if path.endswith(".avi") else {"count": 5067 - lost, "empty": 0, "digest": f"a{5067 - lost}", "start": 0.0, "end": 390.97}
        return {0: video, 1: audio}, {}
    monkeypatch.setattr(hook, "packet_hashes", hashes)
    src, tmp = tmp_path / "Short.avi", tmp_path / ".Short.avi.repack-tmp"
    src.touch(); tmp.touch()
    got, proof = proved(str(src), str(tmp), [], str(tmp_path))
    assert got == fault, got
    if not fault:
        assert proof[1] == {"stream": "audio 1", "codec": "ac3", "method": "packets", "count": 5067, "hash": "a5067", "match": True, "start": [0.0, 0.0],
                            "dropped": {"pts": 390.96, "size": 213}, "times": {"checked": False, "why": "AVI keeps no audio times"}}


def test_packet_hashes_keep_the_digests_without_the_first_packet(monkeypatch, tmp_path):
    """A cut first audio frame is proved by the digest over every packet but the first, and a cut frame at both ends by
    the digest without the first and the last. The md5 and the size of each packet stay too."""
    m = [hook.hashlib.md5(bytes([i])).hexdigest() for i in range(4)]
    lines = ["#tb 0: 1/1000"] + [f"0, {i * 32}, {i * 32}, 32, {z}, {m[i]}" for i, z in enumerate((144, 768, 768, 412))]
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: open(argv[-1], "w").write("\n".join(lines) + "\n")
                        and types.SimpleNamespace(returncode=0, stderr=""))
    s = hook.packet_hashes("/m/x.avi", [1], {}, [], str(tmp_path), 60)[0][1]
    sha = lambda *k: hook.hashlib.sha256("".join(m[i] for i in k).encode()).hexdigest()
    assert (s["digest"], s["but_last"], s["but_first"], s["but_ends"]) == (sha(0, 1, 2, 3), sha(0, 1, 2), sha(1, 2, 3), sha(1, 2))
    assert (s["first"], s["first_pts"], s["last"], s["last_pts"]) == (144, 0.0, 412, 0.096)
    assert s["md5s"] == bytes.fromhex("".join(m)) and list(s["sizes"]) == [144, 768, 768, 412]


@pytest.mark.parametrize("case, fault", [
    ("first", None),        # a 144-byte fragment of a 768-byte AC3 frame opens the stream
    ("both", None),         # a fragment at each end, every other packet matches
    ("first, data differ", "stream audio 1 (ac3) holds 5066 packets in the new file, 5067 in the original"),
    ("video first", "stream video 0 (mpeg4) holds 9764 packets in the new file, 9765 in the original"),
    ("first, start moved", "stream audio 1 starts +0.500 s from the video in the new file, +0.032 s in the original"),
    ("both, TS", None),     # a transport stream keeps audio times, so the times check runs on the packets between the ends
])
def test_a_cut_first_audio_frame_passes_and_is_logged(monkeypatch, tmp_path, case, fault):
    """In an AVI, mkvmerge drops a cut AC3 frame at the start of the stream, and at the end
    too. Both pass for audio. A video stream that loses its first packet, or an audio stream whose other
    packets differ, still fails. The start is checked from the second packet of the original."""
    streams = [{"index": 0, "codec_type": "video", "codec_name": "mpeg4"}, {"index": 1, "codec_type": "audio", "codec_name": "ac3"}]
    ext = ".ts" if case.endswith("TS") else ".avi"
    monkeypatch.setattr(hook, "ff_streams", lambda p: ({".avi": "avi", ".ts": "mpegts"}.get(os.path.splitext(p)[1], "matroska,webm"), streams, 391.0))
    video = {"count": 9765, "empty": 0, "digest": "v", "but_last": "v9", "but_first": "v1", "but_ends": "v1-9", "first": 3000,
             "first_pts": 0.0, "last": 2000, "last_pts": 390.9, "start": 0.0, "end": 391.0}
    audio = {"count": 5067, "empty": 0, "digest": "a", "but_last": "a-last", "but_first": "a-first", "but_ends": "a-ends",
             "first": 144, "first_pts": 0.0, "last": 412, "last_pts": 390.96, "start": 0.0, "end": 391.0,
             "times": [0.0] + [0.032 * i for i in range(1, 5066)] + [390.96]}
    lost = 2 if case.startswith("both") else 1
    new_a = {"count": 5067 - lost, "empty": 0, "digest": "a-ends" if lost == 2 else "a-first", "start": 0.032, "end": 390.93,
             "times": audio["times"][1:5067 - lost + 1]}
    new_v = dict(video)
    if case == "first, data differ":
        new_a["digest"] = "a-other"
    elif case == "video first":
        new_v = {"count": 9764, "empty": 0, "digest": "v1", "start": 0.0, "end": 391.0}
    elif case == "first, start moved":
        new_a["start"] = 0.5

    def hashes(path, maps, bsf, texts, folder, timeout, raw=False):
        return ({0: video, 1: audio} if path.endswith(ext) else {0: new_v, 1: new_a}), {}
    monkeypatch.setattr(hook, "packet_hashes", hashes)
    src, tmp = tmp_path / f"Short{ext}", tmp_path / f".Short{ext}.repack-tmp"
    src.touch(); tmp.touch()
    got, proof = proved(str(src), str(tmp), [], str(tmp_path))
    assert got == fault, got
    if not fault:
        assert proof[1]["dropped_first"] == {"pts": 0.0, "size": 144} and proof[1]["match"] and proof[1]["start"] == [0.032, 0.032]
        assert proof[1].get("dropped") == ({"pts": 390.96, "size": 412} if case.startswith("both") else None)


@pytest.mark.parametrize("case, fault", [
    ("trimmed", None),      # a 480-byte fragment with no header opens the MP3 stream, mkvmerge keeps 96 bytes
    ("same size", "the packet data of stream audio 1 (mp3) differ"),
    ("second packet moved", "stream audio 1 (mp3) lost a trimmed first frame, and its second packet moved -96 ms"),
    ("pcm", "the packet data of stream audio 1 (pcm_s16le) differ"),
    ("grew", "the packet data of stream audio 1 (mp3) differ"),
    ("other packets differ", "the packet data of stream audio 1 (mp3) differ"),
    ("video", "the packet data of stream video 0 (mpeg4) differ"),
])
def test_a_trimmed_first_audio_frame_passes_and_is_logged(monkeypatch, tmp_path, case, fault):
    """An AVI whose first MP3 packet is a cut fragment: mkvmerge keeps only its tail. That passes. A first packet that
    grew or kept its size, other packets that differ, a second packet that moved, PCM or video still fail."""
    streams = [{"index": 0, "codec_type": "video", "codec_name": "mpeg4"},
               {"index": 1, "codec_type": "audio", "codec_name": "pcm_s16le" if case == "pcm" else "mp3"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("avi" if p.endswith(".avi") else "matroska,webm", streams, 1800.0))
    video = {"count": 45000, "empty": 0, "digest": "v", "but_first": "v-rest", "first": 9000, "first_pts": 0.0, "start": 0.0, "end": 1800.0}
    audio = {"count": 52000, "empty": 0, "digest": "a", "but_first": "a-rest", "first": 480, "first_pts": 0.0, "start": 0.0, "end": 1800.0,
             "times": [0.0, 0.026, 0.052]}
    new_a = dict(audio, digest="a2", first={"grew": 600, "same size": 480}.get(case, 96),
                 but_first="a-other" if case == "other packets differ" else "a-rest",
                 times=[0.0, 0.026 - 0.096, 0.052 - 0.096] if case == "second packet moved" else [0.0, 0.026, 0.052])
    new_v = dict(video, digest="v2", first=4000) if case == "video" else video

    def hashes(path, maps, bsf, texts, folder, timeout, raw=False):
        return ({0: video, 1: audio} if path.endswith(".avi") else {0: new_v, 1: new_a}), {}
    monkeypatch.setattr(hook, "packet_hashes", hashes)
    src, tmp = tmp_path / "Show.avi", tmp_path / ".Show.avi.repack-tmp"
    src.touch(); tmp.touch()
    got, proof = proved(str(src), str(tmp), [], str(tmp_path))
    assert got == fault, got
    if not fault:
        assert proof[1]["trimmed_first"] == {"pts": 0.0, "size": [480, 96]} and proof[1]["match"]


MP3_FRAME = b"\xff\xfb\x90\x64" + bytes(range(1, 256)) + bytes(158)   # one 417-byte MPEG audio frame
@pytest.mark.parametrize("case, refused", [
    ("clean", None),
    ("audio count", (1, "stream audio 1 (aac) holds 12 packets in the new file, 10 in the original")),
    ("video data", (0, "the packet data of stream video 0 (h264) differ")),
    ("audio start", (None, "stream audio 1 starts +1.000 s from the video in the new file, +0.000 s in the original")),
    ("video data, audio count", (0, "the packet data of stream video 0 (h264) differ")),   # the first refusal and its stream
])
def test_a_refusal_names_the_stream_of_its_packets(monkeypatch, tmp_path, case, refused):
    """prove() returns its refusal as (stream, why). stream is the ffprobe index of the stream it refuses for its
    packet count or its packet data, which audio_damage() pairs with mkvmerge's warning. Any other refusal names none."""
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}, {"index": 1, "codec_type": "audio", "codec_name": "aac"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("mov,mp4,m4a,3gp,3g2,mj2" if p.endswith(".src") else "matroska,webm", streams, 0.4))
    stats = lambda n, digest, start=0.0: {"count": n, "empty": 0, "digest": digest, "start": start, "end": 0.4,
                                          "times": [start + i * 0.04 for i in range(n)]}
    old = {0: stats(10, "v"), 1: stats(10, "a")}
    new = {0: stats(10, "w" if case.startswith("video data") else "v"),
           1: stats(12, "b") if case.endswith("audio count") else stats(10, "a", 1.0 if case == "audio start" else 0.0)}
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, texts, folder, timeout, raw=False, opts=():
                        (old if path.endswith(".src") else new, {}))
    (tmp_path / "a.src").touch(); (tmp_path / "a.mkv").touch()
    assert hook.prove(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))[0] == refused


LOST_ONE = "stream audio 1 (mp3) holds 999 packets in the new file, 1000 in the original"


@pytest.mark.parametrize("case, fault", [
    ("zeros", None),        # 626 zero bytes before the frame, and the second packet +26 ms, as ffmpeg counts AVI bytes
    ("header", None),       # a 70-byte RIFF header before the frame
    ("zeros, MP4", None),   # a container with audio times: the cut last frame leaves the time check too
    ("header of 128 bytes", None),
    ("header of 129 bytes", LOST_ONE),
    ("other head", LOST_ONE),
    ("short other head", LOST_ONE),   # 70 bytes of a frame, no RIFF header
    ("not the tail", LOST_ONE),
    ("second packet moved", "stream audio 1 (mp3) lost a trimmed first frame, and its second packet moved +60 ms"),
])
def test_a_trimmed_first_frame_may_come_with_a_cut_last_frame(monkeypatch, tmp_path, case, fault):
    """In an AVI, packet 0 of an MP3 stream can hold junk before a whole frame, and the last packet can be a cut frame.
    mkvmerge keeps only the frame after the junk and drops the cut frame. That passes when the new packet 0 is the tail
    of the old one after zeros or a RIFF header of at most 128 bytes, every other packet matches, and the second packet
    keeps its time within 50 ms. A head of other bytes, short or long, or a new packet 0 that is not the tail, fails."""
    head = {"header": b"RIFF" + bytes(66), "header of 128 bytes": b"RIFF" + bytes(124), "header of 129 bytes": b"RIFF" + bytes(125),
            "other head": b"\x01" * 626, "short other head": b"\xff\xfb" + b"\x01" * 68}.get(case, bytes(626))
    old0, new0 = head + MP3_FRAME, MP3_FRAME[:-1] + (b"\x01" if case == "not the tail" else b"\0")
    fam = "mov,mp4,m4a,3gp,3g2,mj2" if case.endswith("MP4") else "avi"
    delta = {"zeros, MP4": 0.0, "second packet moved": 0.06}.get(case, 0.026)
    streams = [{"index": 0, "codec_type": "video", "codec_name": "mpeg4"}, {"index": 1, "codec_type": "audio", "codec_name": "mp3"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: (fam if p.endswith(".src") else "matroska,webm", streams, 26.0))
    video = {"count": 650, "empty": 0, "digest": "v", "start": 0.0, "end": 26.0, "times": [i * 0.04 for i in range(650)]}
    old = {"count": 1000, "empty": 0, "digest": "a", "but_first": "a-first", "but_ends": "a-ends", "first": len(old0), "first_pts": 0.0,
           "last": 200, "last_pts": 25.974, "start": 0.0, "end": 26.0, "times": [i * 0.026 for i in range(1000)]}
    new = {"count": 999, "empty": 0, "digest": "b", "but_first": "a-ends", "first": len(new0), "start": 0.0, "end": 25.974,
           "times": [0.0] + [round(i * 0.026 + delta, 3) for i in range(1, 999)]}
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, texts, folder, timeout, raw=False, opts=():
                        ({0: video, 1: old if path.endswith(".src") else new}, {}))
    monkeypatch.setattr(hook, "packet_data", lambda path, index, bsf, n, timeout: old0 if path.endswith(".src") else new0)
    (tmp_path / "a.src").touch(); (tmp_path / "a.mkv").touch()
    got, proof = proved(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))
    assert got == fault, got
    if not fault:
        junk = "header" if case.startswith("header") else "zero"
        assert proof[1]["trimmed_first"] == {"pts": 0.0, "size": [len(old0), len(new0)], "junk": junk} and proof[1]["match"], proof[1]
        assert proof[1]["dropped"] == {"pts": 25.974, "size": 200}


@pytest.mark.parametrize("case, fault", [
    ("cut frame, start moved", "stream audio 1 starts +0.500 s from the video in the new file, +0.000 s in the original"),
    ("edit list, start moved", "stream audio 1 starts +0.500 s from the video in the new file, +0.000 s in the original"),
    ("times go back 0.3 s", "a packet of stream audio 1 (aac) moved 300 ms against its stream's start"),
    ("times 1 ms off", None),
    ("AVI audio drifts 26 ms", None),   # an AVI keeps no audio times, ffmpeg counts bytes, mkvmerge samples
])
def test_every_stream_keeps_its_start_and_its_packet_times(monkeypatch, tmp_path, case, fault):
    """A stream that lost its cut last frame, or kept a sample an edit list hides, still has its
    start checked: one that also starts 0.5 s later fails. And every packet keeps its time against its stream's start:
    a TS whose audio times go back 0.3 s at 1 s plays 0.3 s late once mkvmerge made them continuous. Matroska's
    millisecond rounding passes."""
    fam = "mov,mp4,m4a" if case.startswith("edit") else "avi" if case.startswith("AVI") else "mpegts"
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}, {"index": 1, "codec_type": "audio", "codec_name": "aac"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: (fam if p.endswith(".src") else "matroska,webm", streams, 2.0))
    monkeypatch.setattr(hook, "edit_list_sample", lambda *a: True)
    video = {"count": 50, "empty": 0, "digest": "v", "start": 0.0, "end": 2.0, "times": [i * 0.04 for i in range(50)]}
    times = [i * 0.04 for i in range(50)]
    old = {"count": 50, "empty": 0, "digest": "a50", "but_last": "a49", "last": 100, "last_pts": 1.96, "start": 0.0, "end": 2.0, "times": times}
    new = dict(old, but_last=None)
    if case == "cut frame, start moved":
        new = dict(old, count=49, digest="a49", start=0.5, times=[t + 0.5 for t in times[:49]])
    elif case == "edit list, start moved":
        new = dict(old, count=51, digest="a51", but_last="a50", last_pts=2.5, start=0.5, times=[t + 0.5 for t in times] + [2.5])
    elif case == "times go back 0.3 s":   # the original goes back at 1 s, the new file runs on as mkvmerge made it
        old = dict(old, times=times[:25] + [t - 0.3 for t in times[25:]])
    elif case.startswith("AVI"):
        new = dict(old, times=[t * 1.0003 for t in times], end=2.0)
    else:
        new = dict(old, times=[t + (0.001 if i % 2 else 0) for i, t in enumerate(times)])
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, texts, folder, timeout, raw=False, opts=():
                        ({0: video, 1: old if path.endswith(".src") else new}, {}))
    monkeypatch.setattr(hook, "stored_times", lambda path, index, timeout: new["times"])   # the times mkvmerge stored
    src, tmp = tmp_path / "a.src", tmp_path / "a.tmp"
    src.touch(); tmp.touch()
    got, proof = proved(str(src), str(tmp), [], str(tmp_path))
    assert (got or "").startswith(fault or "") and bool(got) == bool(fault), got
    assert proof[1]["match"] is (fault is None)
    assert proof[1]["times"] == {"checked": False, "why": "AVI keeps no audio times"} if case.startswith("AVI") else "times" in proof[1] or fault


FRAME = 1001 / 24000   # one frame at 23.976 fps


@pytest.mark.parametrize("case", ["clean", "read moved", "stored moved", "edit list, read moved"])
def test_a_failed_time_check_reads_the_stored_times_again(monkeypatch, tmp_path, case):
    """Matroska stores no decode times. ffmpeg guesses them, and one frame stored far ahead of the frames it displays
    after breaks the guess. ffmpeg's muxer then moves a few packet times of its read by one frame near one point. ffprobe
    only demuxes, and its read of the same file is clean. So a failed time check reads the stored times of that stream
    again, and only that second check decides. Stored times that really moved still fail. A clean first check runs
    no second read. When the new file holds the one sample an MP4 edit list hides, the second check leaves it out too."""
    order = [0, 3, 1, 2]   # an I or P frame, then the two B-frames it displays after, in file order
    src = [(4 * (i // 4) + order[i % 4]) * FRAME for i in range(400)]
    stored = [round(t, 3) for t in src]   # Matroska keeps milliseconds
    read = [t + FRAME if 200 <= i < 203 else t for i, t in enumerate(stored)]
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("mov,mp4,m4a,3gp,3g2,mj2" if p.endswith(".mp4") else "matroska,webm", streams, 16.7))
    stats = lambda times: {"count": 400, "empty": 0, "digest": "v", "start": 0.0, "end": 16.7, "times": times}
    extra = [16.683] if case.startswith("edit list") else []   # the sample the edit list hides, last in the new file
    new = lambda times: dict(stats(times + extra), count=401, digest="v+", but_last="v", last=900, last_pts=16.683) if extra else stats(times)
    monkeypatch.setattr(hook, "edit_list_sample", lambda *a: True)
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, texts, folder, timeout, raw=False, opts=():
                        ({0: stats(src) if path.endswith(".mp4") else new(stored if case == "clean" else read)}, {}))
    probes = []

    def run(argv, **kw):   # ffprobe's demux of the new file: pts_time,size per packet, and one packet with no data
        probes.append(argv)
        rows = [f"{t:.6f},{900 + i}" for i, t in enumerate((read if case == "stored moved" else stored) + extra)] + ["16.700000,0"]
        return types.SimpleNamespace(returncode=0, stdout="\n".join(rows) + "\n", stderr="")
    monkeypatch.setattr(hook.subprocess, "run", run)
    (tmp_path / "Film.mp4").touch(); (tmp_path / "new.mkv").touch()
    fault, proof = proved(str(tmp_path / "Film.mp4"), str(tmp_path / "new.mkv"), [], str(tmp_path))
    times = proof[0]["times"]
    if case == "clean":
        assert fault is None and probes == [] and "reread" not in times, (fault, times)
        return
    assert 41 < times["worst_ms"] < 43 and probes[0][probes[0].index("-select_streams") + 1] == "0" and probes[0][-1].endswith("new.mkv")
    if case.endswith("read moved"):
        assert fault is None and proof[0]["match"] and times["reread"]["worst_ms"] < 1, (fault, times)
        assert ("edit_list" in proof[0]) == bool(extra)
    else:
        assert fault.startswith("a packet of stream video 0 (h264) moved 42 ms") and 41 < times["reread"]["worst_ms"] < 43, fault


def packets_of(datas, times, step):
    """packet_hashes() of one stream from the data and the time of each packet, with their md5s and sizes."""
    md5s = b"".join(hook.hashlib.md5(d).digest() for d in datas)
    return {"count": len(datas), "empty": 0, "digest": hook.hashlib.sha256(md5s).hexdigest(), "md5s": bytearray(md5s),
            "sizes": hook.array.array("L", map(len, datas)), "times": list(times), "start": round(min(times), 3),
            "end": round(max(times) + step, 3)}


def fake_proof_reads(monkeypatch, fam, streams, old, new, packets=None):
    """prove() of a.src against a.mkv in the current folder with fake reads: ff_streams() gives fam and streams,
    packet_hashes() old or new, and packet_data() the first n packets of packets, joined. Returns the list of
    packet_data() reads."""
    open("a.src", "w").close(); open("a.mkv", "w").close()
    monkeypatch.setattr(hook, "ff_streams", lambda p: (fam if p.endswith(".src") else "matroska,webm", streams, 20.0))
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, texts, folder, timeout, raw=False, opts=():
                        (old if path.endswith(".src") else new, {}))
    monkeypatch.setattr(hook, "stored_times", lambda path, index, timeout: new[index]["times"])   # as ffmpeg read them
    reads = []
    monkeypatch.setattr(hook, "packet_data", lambda path, index, bsf, n, timeout: reads.append((index, n)) or b"".join(packets[:n]))
    return reads


@pytest.mark.parametrize("lost, other, fault", [
    (3, None, None),   # the last 3 frames
    (4, None, "stream video 0 (h264) holds 196 packets in the new file, 200 in the original"),
    (2, 50, "stream video 0 (h264) holds 198 packets in the new file, 200 in the original"),   # and a frame in the middle differs
    (-2, None, "stream video 0 (h264) holds 198 packets in the new file, 200 in the original"),   # the first 2 frames
])
def test_up_to_three_video_packets_may_go_at_the_end(monkeypatch, tmp_path, lost, other, fault):
    """The new file may lack up to 3 video packets at its end when every other packet matches in order. The proof
    names each lost packet with its time and size. A fourth lost packet, another packet that differs, or a packet lost
    at the start fails."""
    monkeypatch.chdir(tmp_path)
    frame = 1001 / 24000
    datas, times = [bytes([i % 251]) * (100 + i) for i in range(200)], [i * frame for i in range(200)]
    at = slice(-lost, None) if lost < 0 else slice(None, 200 - lost)   # a negative count loses the first packets
    kept = datas[at]
    if other:
        kept[other] = b"x" + kept[other][1:]
    fake_proof_reads(monkeypatch, "mov,mp4,m4a,3gp,3g2,mj2", [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
                     {0: packets_of(datas, times, frame)}, {0: packets_of(kept, [round(t, 3) for t in times[at]], frame)})
    got, proof = proved(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))
    assert got == fault, got
    if not fault:
        assert proof[0]["match"] and proof[0]["dropped_end"] == [{"pts": round(times[i], 3), "size": 100 + i, "kind": "lost"} for i in (197, 198, 199)]


FRAME_AC3 = 0.032
AUDIO_JUNK = {   # (the original's junk at the start, at the end, whether the new file keeps the cut last frame, its move)
    "zero packets at the end": ([], [bytes(768)] * 6, True, None),
    "16 zero packets at the end": ([], [bytes(768)] * 16, True, None),
    "17 zero packets at the end": ([], [bytes(768)] * 17, True, None),
    "header and zeros at the start, cut last frame": ([b"RIFF" + bytes(66), bytes(400)], [], False, None),
    "header of 128 bytes": ([b"RIFF" + bytes(124), bytes(400)], [], True, None),
    "header of 129 bytes": ([b"RIFF" + bytes(125), bytes(400)], [], True, None),
    "zero bytes at the start, audio late": ([bytes(1599)], [], True, 0.1),
    "two frames lost at the start": ([b"\x0b\x77" + bytes([1]) * 98, b"\x0b\x77" + bytes([2]) * 98], [], True, None),
    "two frames lost at the end": ([], [b"\x0b\x77" + bytes([1]) * 700, b"\x0b\x77" + bytes([2]) * 700], True, None),
}


@pytest.mark.parametrize("case, fault", [
    ("zero packets at the end", None),
    ("16 zero packets at the end", None),
    ("17 zero packets at the end", "stream audio 1 (ac3) holds 300 packets in the new file, 317 in the original"),
    ("header and zeros at the start, cut last frame", None),
    ("header of 128 bytes", None),
    ("header of 129 bytes", "stream audio 1 (ac3) holds 300 packets in the new file, 302 in the original"),
    ("zero bytes at the start, audio late", "stream audio 1 starts +0.132 s from the video in the new file, +0.032 s in the original"),
    ("two frames lost at the start", "stream audio 1 (ac3) holds 300 packets in the new file, 302 in the original"),
    ("two frames lost at the end", "stream audio 1 (ac3) holds 300 packets in the new file, 302 in the original"),
])
def test_audio_may_lose_junk_and_cut_frames_at_its_ends(monkeypatch, tmp_path, case, fault):
    """Audio may lose packets of zero bytes and a stray RIFF header at its start, zero packets at its end, and a cut
    frame at each end, when every other packet matches in order. The kept packets keep their start and their times:
    audio that runs 100 ms late for its first 0.5 s after a lost run of 1,599 zero bytes fails. Two real frames lost
    at the start fail, and a read of the first packets shows they are no header. So do two real frames lost at the end,
    a header over 128 bytes and more than 16 lost packets. Each junk packet has its own time before the first frame, so
    the kept audio starts at its first kept packet."""
    monkeypatch.chdir(tmp_path)
    head, tail, cut_kept, late = AUDIO_JUNK[case]
    frames = [b"\x0b\x77" + bytes([i % 251]) * (766 - i % 7) for i in range(300)]
    cut = [] if cut_kept else [b"\x0b\x77" + bytes(80)]
    old_datas = head + frames + cut + tail
    old_times = [i * FRAME_AC3 for i in range(len(head) + 300 + len(cut) + len(tail))]
    new_times = [round((len(head) + i) * FRAME_AC3 + (late if late and i * FRAME_AC3 < 0.5 else 0), 3) for i in range(300)]
    video = packets_of([bytes([i % 251]) * 900 for i in range(240)], [i * 0.04 for i in range(240)], 0.04)
    reads = fake_proof_reads(monkeypatch, "mpegts", [{"index": 0, "codec_type": "video", "codec_name": "mpeg2video"},
                                                     {"index": 1, "codec_type": "audio", "codec_name": "ac3"}],
                             {0: video, 1: packets_of(old_datas, old_times, FRAME_AC3)}, {0: video, 1: packets_of(frames, new_times, FRAME_AC3)},
                             old_datas)
    got, proof = proved(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))
    assert got == fault, got
    # one read of the first packets, as many as the stream lost, and only for a non-zero packet before the first frame
    assert reads == {"header and zeros at the start, cut last frame": [(1, 3)], "header of 128 bytes": [(1, 2)],
                     "two frames lost at the start": [(1, 2)]}.get(case, []), reads
    if case.endswith("zero packets at the end") and not fault:
        assert [d["kind"] for d in proof[1]["dropped_end"]] == ["zero"] * len(tail) and "dropped_start" not in proof[1] and proof[1]["match"]
    elif case == "header of 128 bytes":
        assert [(d["kind"], d["size"]) for d in proof[1]["dropped_start"]] == [("header", 128), ("zero", 400)] and proof[1]["start"] == [0.064, 0.064]
    elif not fault:
        assert [(d["kind"], d["size"]) for d in proof[1]["dropped_start"]] == [("header", 70), ("zero", 400)]
        assert [(d["kind"], d["size"]) for d in proof[1]["dropped_end"]] == [("cut", 82)] and proof[1]["start"] == [0.064, 0.064]


@pytest.mark.parametrize("move, other, fault", [
    (1, 0, None),   # mkvmerge spaces the two packets of one time one frame apart
    (2, 0, "a packet of stream audio 1 (aac) moved 43 ms against its stream's start"),
    (1, 0.005, "a packet of stream audio 1 (aac) moved 26 ms against its stream's start"),   # and a later packet moves 5 ms
])
def test_audio_packets_that_share_a_time_may_move_one_frame(monkeypatch, tmp_path, move, other, fault):
    """In an MP4 the first two AAC packets may carry one time. mkvmerge moves the first one a frame earlier. Against the
    video's start, a packet that shares its time may move one frame, and every other packet 2 ms. Two frames, or
    another packet that moves 5 ms, fail."""
    monkeypatch.chdir(tmp_path)
    frame = 1024 / 48000
    datas = [bytes([i % 251]) * 300 for i in range(500)]
    old = [0.0, 0.0] + [i * frame for i in range(1, 499)]
    new = [round(-move * frame, 3), 0.0] + [round(i * frame + (other if i == 100 else 0), 3) for i in range(1, 499)]
    video = packets_of([bytes([i % 251]) * 900 for i in range(250)], [i * 0.04 for i in range(250)], 0.04)
    fake_proof_reads(monkeypatch, "mov,mp4,m4a,3gp,3g2,mj2", [{"index": 0, "codec_type": "video", "codec_name": "mpeg4"},
                                                              {"index": 1, "codec_type": "audio", "codec_name": "aac"}],
                     {0: video, 1: packets_of(datas, old, frame)}, {0: video, 1: packets_of(datas, new, frame)})
    got, proof = proved(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))
    assert (got or "").startswith(fault or "") and bool(got) == bool(fault), got
    assert proof[1]["times"]["shared"]["packets"] == 2 and proof[1]["times"]["shared"]["frame_ms"] == 21.3


def test_video_packets_that_share_a_time_still_fail(monkeypatch, tmp_path):
    """The shared-time rule is for audio only. A video stream whose first two packets share a time, and whose second
    packet moves one frame in the new file, fails the time check."""
    monkeypatch.chdir(tmp_path)
    frame = 1001 / 24000
    datas = [bytes([i % 251]) * 900 for i in range(200)]
    old = [0.0, 0.0] + [i * frame for i in range(2, 200)]
    new = [0.0] + [round(i * frame, 3) for i in range(1, 200)]
    fake_proof_reads(monkeypatch, "mov,mp4,m4a,3gp,3g2,mj2", [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
                     {0: packets_of(datas, old, frame)}, {0: packets_of(datas, new, frame)})
    got, proof = proved(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))
    assert (got or "").startswith("a packet of stream video 0 (h264) moved 42 ms") and "shared" not in proof[0]["times"], got


SEI_UNIT = bytes.fromhex("4e0181010f80")   # a prefix SEI in the codec header
SLICE_UNIT = bytes.fromhex("2601af") + bytes(range(1, 60))


@pytest.mark.parametrize("case", ["header unit", "other unit", "slice lost", "clean"])
def test_an_hevc_packet_0_may_hold_a_unit_of_the_codec_header(monkeypatch, tmp_path, case):
    """mkvmerge copies the units of the HEVC codec header into packet 0, and the proof filter takes out only the
    parameter sets. Packet 0 passes when it holds the original's packet 0 and units of the header, byte for byte, and
    every other packet matches. A unit that is not in the header fails, and so does a packet 0 that gains the header's
    SEI and loses a slice. A clean stream reads no packet."""
    monkeypatch.chdir(tmp_path)
    start = b"\0\0\0\1"
    old = start + SLICE_UNIT + (start + SLICE_UNIT[:3] + bytes(range(60, 90)) if case == "slice lost" else b"")
    new = {"header unit": start + SEI_UNIT + start + SLICE_UNIT, "other unit": start + SEI_UNIT[:-2] + b"\x0e\x80" + start + SLICE_UNIT,
           "slice lost": start + SEI_UNIT + start + SLICE_UNIT, "clean": old}[case]
    stats = lambda first, digest: {"count": 48, "empty": 0, "digest": digest, "but_first": "rest", "first": len(first), "start": 0.0,
                                   "end": 2.0, "times": [i * 0.04 for i in range(48)]}
    fake_proof_reads(monkeypatch, "mov,mp4,m4a,3gp,3g2,mj2", [{"index": 0, "codec_type": "video", "codec_name": "hevc"}],
                     {0: stats(old, "d-old")}, {0: stats(new, "d-old" if case == "clean" else "d-new")})
    reads = []
    monkeypatch.setattr(hook, "packet_data", lambda path, index, bsf, n, timeout: reads.append((path[-3:], bsf, n)) or (old if path.endswith(".src") else new))
    monkeypatch.setattr(hook, "hvcc_units", lambda path, index, timeout: [bytes.fromhex("4001"), bytes.fromhex("4201"), bytes.fromhex("4401"), SEI_UNIT])
    got, proof = proved(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))
    if case == "clean":
        assert got is None and reads == [] and "header_units" not in proof[0]
    elif case in ("other unit", "slice lost"):
        assert got == "the packet data of stream video 0 (hevc) differ", got
    else:
        assert got is None and proof[0]["header_units"] == ["4e0181010f80"], (got, proof)
        assert reads == [("src", hook.NO_AUD["hevc"], 1), ("mkv", hook.NO_AUD["hevc"], 1)]


def test_a_real_hevc_remux_passes_with_the_header_sei_in_packet_0(tmp_path):
    """x265 writes an SEI into the HEVC codec header of an MP4, and mkvmerge copies it into packet 0 of the new file."""
    if not (shutil.which("mkvmerge") and shutil.which("ffmpeg")) or "libx265" not in REAL_RUN(["ffmpeg", "-hide_banner", "-encoders"],
                                                                                                capture_output=True, text=True).stdout:
        pytest.skip("needs ffmpeg with libx265 and mkvmerge")
    src, out = tmp_path / "Clip.mp4", tmp_path / "new.mkv"
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=2", "-c:v", "libx265", "-x265-params",
              "log-level=none", "-tag:v", "hvc1", str(src)], check=True)
    REAL_RUN(["mkvmerge", "-q", "--disable-lacing", "-o", str(out), str(src)], check=True)
    fault, proof = proved(str(src), str(out), [], str(tmp_path))
    assert fault is None and proof[0]["header_units"][0].startswith("4e01"), (fault, proof)


@pytest.mark.parametrize("case, fault", [
    ("zero cue", None),
    ("runs into the next cue", "the mov_text stream 1 "),
    ("another cue moved", "the mov_text stream 1 "),
])
def test_a_zero_length_cue_may_get_a_length(monkeypatch, tmp_path, case, fault):
    """An MP4 timed-text cue may start and end at one time, beside another cue at that start. mkvmerge gives it a
    length, and the sort then puts the pair the other way round. It passes when it keeps its start and text and ends
    before the next cue. One that runs into the next cue fails, and so does any other cue that moved."""
    (tmp_path / "a.src").touch(); (tmp_path / "a.mkv").touch()
    nxt = "00:01:01,800" if case == "runs into the next cue" else "00:01:04,000"
    srt = lambda zero_end, open_end: (f"1\n00:01:00,000 --> {zero_end}\n- Who is it?\n\n2\n00:01:00,000 --> {open_end}\n- Open the door.\n\n"
                                      f"3\n{nxt} --> 00:01:06,000\nThe next line\n")
    old, new = srt("00:01:00,000", "00:01:01,500"), srt("00:01:02,000", "00:01:01,700" if case == "another cue moved" else "00:01:01,500")
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("mov,mp4" if p.endswith(".src") else "matroska,webm",
                                                       [{"index": 0, "codec_type": "video", "codec_name": "h264"},
                                                        {"index": 1, "codec_type": "subtitle", "codec_name": "mov_text" if p.endswith(".src") else "subrip"}], 90.0))
    video = {"count": 10, "empty": 0, "digest": "v", "start": 0.0, "end": 90.0, "times": [i * 9.0 for i in range(10)]}
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, texts, folder, timeout, raw=False, opts=():
                        ({0: video}, {i: old if path.endswith(".src") else new for i in texts}))
    got, proof = proved(str(tmp_path / "a.src"), str(tmp_path / "a.mkv"), [], str(tmp_path))
    assert (got or "").startswith(fault or "") and bool(got) == bool(fault), got
    assert proof[1].get("zero_length") == ([60.0] if case != "runs into the next cue" else None), proof[1]


def test_radarr_reads_the_extras_and_the_original_path_from_its_api(monkeypatch):
    """Radarr's extrafile API lists the subtitles, metadata and other files of one movie, as the database holds them.
    The conversion hides the ones of the old record, and never metadata. The original download path is on the record.
    A read that fails raises, so no conversion runs blind."""
    rows = [{"relativePath": "a.en.srt", "movieFileId": 11, "type": "subtitle"}, {"relativePath": "a.nfo", "movieFileId": 11, "type": "metadata"},
            {"relativePath": "Extras/a.pdf", "movieFileId": 11, "type": "other"}, {"relativePath": "b.pdf", "movieFileId": 12, "type": "other"},
            {"relativePath": "poster.jpg", "movieFileId": None, "type": "metadata"}]
    reads = []
    monkeypatch.setattr(hook, "arr", lambda app, p: reads.append((app, p)) or rows)
    radarr = hook.ARR["radarr"]
    assert radarr.extra_rows(7)[:2] == [("a.en.srt", 11, "subtitle"), ("a.nfo", 11, "metadata")] and reads == [("radarr", "extrafile?movieId=7")]
    assert radarr.extras(7, 11, "/m/Film A/a.mp4", "/m/Film A", {7: True}) == ["/m/Film A/Extras/a.pdf", "/m/Film A/a.en.srt"]
    assert radarr.original(7, {"id": 11, "originalFilePath": "Film.A.1979.1080p-GRP/a.mp4"}) == "Film.A.1979.1080p-GRP/a.mp4"
    assert radarr.original(7, {"id": 11}) is None
    monkeypatch.setattr(hook, "arr", lambda app, p: (_ for _ in ()).throw(ConnectionError("Radarr restarts")))
    with pytest.raises(ConnectionError):
        radarr.extras(7, 11, "/m/Film A/a.mp4", "/m/Film A", {7: True})


def test_sonarr_takes_the_extras_by_its_own_rule(tmp_path, monkeypatch):
    """Sonarr 4 has no API for extra files. A rescan tracks each file under the series folder that is no video, that the
    scan does not leave out, and whose name parses as the episodes of one file. So the walk asks Sonarr's parse API about
    each such file. A file beside the video that starts with its base name needs no parse, and so does a sidecar of
    another video in its folder that Sonarr lists, which stays where it is. A sidecar of a leftover video that Sonarr
    does not list gets the parse, and hides when it parses as this file's episodes. A subtitle with another
    name and a bonus PDF in a sibling folder hide too, and a file of another episode stays. The names Sonarr tracks as
    metadata stay out, as Radarr's metadata does. An .nfo is metadata only when it holds a Kodi tag, and Sonarr reads at
    most 10 MB of it."""
    home = tmp_path / "Show"
    folder, stem = home / "Season 02", "Show - S02E01 - Title"
    for sub in ("Season 02/Extras", "Bonus Material/metadata", "Featurettes", ".hidden"):
        (home / sub).mkdir(parents=True)
    for n in (".mp4", ".mkv", ".en.srt", ".en.forced.srt", ".pdf", ".txt", ".nfo", "-thumb.jpg", "-thumb.PNG", ".xml", ".jpg", ".metathumb"):
        (folder / (stem + n)).write_text("<release/>")
    for n in ("Season 02/" + stem + "0.srt", "Season 02/Show.S02E01.720p.WEB.en.srt", "Season 02/Show - S02E02 - Other.mkv",
              "Season 02/Show - S02E02 - Other.en.srt", "Season 02/Show - S02E02 - Other.nfo", "Season 02/Show - S02E03 - Third.en.srt",
              "Season 02/Show.S02E01.SDTV.mkv", "Season 02/Show.S02E01.SDTV.en.srt", "Season 02/S02E01.png",
              "Season 02/Show.S02E01.cover.jpg", "Season 02/Show.S02E01-trailer.srt", "Season 02/Thumbs.db", "Season 02/Extras/Show.S02E01.en.srt",
              "Bonus Material/Show S02E01 notes.pdf", "Bonus Material/metadata/Show S02E01.jpg", "Featurettes/Show S02E01.pdf",
              ".hidden/Show S02E01.srt", "notes.txt"):
        (home / n).write_text("x")
    (folder / (stem + ".d")).mkdir()   # a folder never hides
    parsed, read, real = [], [], hook.Sonarr.metadata
    monkeypatch.setattr(hook.Sonarr, "metadata", lambda self, p: read.append(os.path.relpath(p, home)) or real(self, p))

    def parse(app, p):   # Sonarr's parse API: a name with S02E01 is episode 31, one with S02E02 is episode 32
        if p == "episodefile?seriesId=5":   # the video and the other episode's, never the leftover
            return [{"id": 9, "path": str(folder / (stem + ".mp4"))}, {"id": 10, "path": str(home / "Season 02/Show - S02E02 - Other.mkv")}]
        q = parse_qs(p.split("?", 1)[1])
        parsed.append(os.path.relpath(q["path"][0], home))
        eps = [31] if "S02E01" in q["title"][0] else [32] if "S02E02" in q["title"][0] else []
        return {"episodes": [{"id": i} for i in eps]}
    monkeypatch.setattr(hook, "arr", parse)
    video = str(folder / (stem + ".mp4"))
    extras = lambda: [os.path.relpath(p, home) for p in hook.ARR["sonarr"].extras(5, 9, video, str(home), {31: True})]
    assert extras() == ["Bonus Material/Show S02E01 notes.pdf", "Bonus Material/metadata/Show S02E01.jpg", f"Season 02/{stem}.en.forced.srt",
                        f"Season 02/{stem}.en.srt", f"Season 02/{stem}.nfo", f"Season 02/{stem}.pdf", f"Season 02/{stem}.txt",
                        f"Season 02/{stem}0.srt", "Season 02/Show.S02E01.720p.WEB.en.srt", "Season 02/Show.S02E01.SDTV.en.srt"]
    assert sorted(parsed) == ["Bonus Material/Show S02E01 notes.pdf", "Bonus Material/metadata/Show S02E01.jpg", f"Season 02/{stem}0.srt",
                              "Season 02/Show - S02E03 - Third.en.srt", "Season 02/Show.S02E01.720p.WEB.en.srt", "Season 02/Show.S02E01.SDTV.en.srt",
                              "notes.txt"]
    assert not [x for x in read if "Other" in x] and "Season 02/S02E01.png" in read   # a listed video's sidecars cost no read
    (folder / (stem + ".nfo")).write_text("<?xml?>\n<episodedetails>\n</episodedetails>")   # Kodi's own .nfo
    assert f"Season 02/{stem}.nfo" not in extras()
    with open(folder / (stem + ".nfo"), "ab") as f:   # sparse, so it costs no disk
        f.truncate((10 << 20) + 1)
    assert f"Season 02/{stem}.nfo" in extras()


def test_the_time_limit_ends_the_sonarr_extras_walk(tmp_path, monkeypatch):
    """A conversion of an import walks the series folder on the NAS under the job's time limit. A long series lists
    many folders and stats each file, so the limit ends the walk between two of them."""
    home = tmp_path / "Show"
    for k in range(1, 11):
        (home / f"Season {k:02}").mkdir(parents=True)
        (home / f"Season {k:02}" / f"Show - S{k:02}E01.en.srt").write_text("x")
    monkeypatch.setattr(hook, "arr", lambda app, p: [] if p.startswith("episodefile?") else {"episodes": []})
    clock = [1000.0]
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 1) or clock[0])   # each look takes a second
    walk = lambda: hook.ARR["sonarr"].extras(5, 9, str(home / "Season 01" / "Show - S01E01.mkv"), str(home), {31: True})
    hook.DEADLINE.start(10)
    with pytest.raises(hook.arr_meta.OutOfTime):
        walk()
    assert walk() == [str(home / "Season 01" / "Show - S01E01.en.srt")]   # with no limit it walks every folder


@pytest.mark.parametrize("case", ["free", "taken before", "taken since the check", "no hard links"])
def test_a_hidden_extra_never_replaces_a_file_at_its_name(tmp_path, monkeypatch, case):
    """An extra goes back by a hard link, then the hidden name goes. A file written at its name after the check fails
    the link, where a rename would replace it. The extra then stays hidden, as for a name taken before the check. A
    file system with no hard links gets the rename."""
    hidden, name = tmp_path / hook.CFG.hide_dir / "a.en.srt", tmp_path / "a.en.srt"
    hidden.parent.mkdir()
    hidden.write_text("extra")
    if case == "taken before":
        name.write_text("new")
    if case == "taken since the check":
        real = os.path.lexists
        monkeypatch.setattr(hook.os.path, "lexists", lambda p: (name.write_text("new") and False) if p == str(name) else real(p))
    if case == "no hard links":
        monkeypatch.setattr(hook.os, "link", lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")))
    left = hook.show_extras([[str(hidden), str(name)]])
    if case.startswith("taken"):
        assert left == [f"{name} is taken, the extra stays at {hidden}"] and name.read_text() == "new" and hidden.read_text() == "extra"
    else:
        assert left == [] and name.read_text() == "extra" and not hidden.parent.exists()


def sonarr_history(monkeypatch, rows, added="2020-05-01T12:00:00Z"):
    """Sonarr's import history of series 5 and the episodes of file 9, 31 and 32. Returns ARR["sonarr"].original() of
    file 9 with dateAdded added."""
    api = {"history/series?seriesId=5&eventType=3": rows, "episode?episodeFileId=9": [{"id": 31}, {"id": 32}]}
    monkeypatch.setattr(hook, "arr", lambda app, p: api[p])
    return hook.ARR["sonarr"].original(5, {"id": 9, "dateAdded": added})


def row(ep, seconds, path, fid=None):
    """One import row of episode ep, about seconds after 12:00:00 in Sonarr's date format, with or without a fileId."""
    t = datetime.datetime(2020, 5, 1, 12, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return {"episodeId": ep, "date": t.strftime("%Y-%m-%dT%H:%M:%S.%f7Z"), "data": {"droppedPath": path, **({"fileId": str(fid)} if fid else {})}}


def test_an_import_row_with_no_file_id_counts_by_its_episode_and_date(monkeypatch):
    """An older Sonarr wrote its import rows with no fileId. The row of one of the file's episodes with the date
    nearest the record's dateAdded counts, within 60 seconds. A row with the file's own fileId wins. A row with the
    fileId of another file, a row of another episode, and a row out of the window never count."""
    others = [row(31, 0, "/dl/other-file.mkv", fid=8), row(33, 1, "/dl/other-episode.mkv"), row(32, 61, "/dl/late.mkv")]
    assert sonarr_history(monkeypatch, others + [row(31, -58, "/dl/near.mkv"), row(32, 45, "/dl/nearest.mkv")]) == "/dl/nearest.mkv"
    assert sonarr_history(monkeypatch, others + [row(32, 45, "/dl/nearest.mkv"), row(31, 3600, "/dl/own.mkv", fid=9)]) == "/dl/own.mkv"
    assert sonarr_history(monkeypatch, others) is None
    assert sonarr_history(monkeypatch, others + [row(31, -59, "/dl/before.mkv")]) == "/dl/before.mkv"
    assert sonarr_history(monkeypatch, others + [row(31, 60, "/dl/edge.mkv")]) == "/dl/edge.mkv"   # 60 s is inside
    assert sonarr_history(monkeypatch, others + [row(31, -60.5, "/dl/over.mkv")]) is None


@pytest.mark.parametrize("history, before", [("match", "Show.S02E01.1080p.WEB.AAC2.0-GRP.mp4"), ("none", "Show - S02E01.mp4"),
                                             ("scene name", "Show.S02E01.720p.HDTV.x264-OLD")])
def test_sonarr_takes_the_original_path_from_its_import_history(monkeypatch, history, before):
    """Sonarr's API leaves out a file's OriginalFilePath. The history of the import that made the file record holds
    the same file name in droppedPath, under its fileId. The score check reads it when the record has no scene name.
    With no such import in the history, the check scores the file name, as it does for a record with no
    OriginalFilePath. Here the download name scores Format A, and the file names score nothing."""
    rows = [{"episodeId": 31, "data": {"fileId": "8", "droppedPath": "/dl/Show.S02E01.HDTV-OLD/a.mp4"}}, {"episodeId": 31, "data": {}},
            {"episodeId": 31, "data": {"fileId": "9" if history != "none" else "7", "droppedPath": "/dl/Show.S02E01.1080p.WEB.AAC2.0-GRP.mp4"}}]
    titles = []

    def app(a, p):
        if p == "history/series?seriesId=5&eventType=3": return rows
        if p == "series/5": return {"id": 5, "qualityProfileId": 4}
        if p == "qualityprofile/4": return {"formatItems": [{"format": 1, "name": "Format A", "score": 5}]}
        title = parse_qs(p.split("?", 1)[1])["title"][0]
        titles.append(title)
        return {"customFormats": [{"id": 1, "name": "Format A"}] if title in ("Show.S02E01.1080p.WEB.AAC2.0-GRP.mp4", "Show.S02E01.720p.HDTV.x264-OLD") else []}
    monkeypatch.setattr(hook, "arr", app)
    old = {"id": 9, "path": "/tv/Show/Season 02/Show - S02E01.mp4", **({"sceneName": before} if history == "scene name" else {})}
    assert hook.ARR["sonarr"].original(5, old) == (None if history == "none" else "/dl/Show.S02E01.1080p.WEB.AAC2.0-GRP.mp4")
    why = hook.score_refuses("sonarr", 5, old, "/tv/Show/Season 02/Show - S02E01.mkv")
    assert titles[0] == before and (why is None) == (history == "none"), (titles, why)
    if why:
        assert f"{before!r} gives ['Format A']" in why and "lost Format A (+5)" in why


@pytest.mark.parametrize("full, ok", [(("a56934", 56934), True), (("a56933", 56933), False)])
def test_a_sample_the_mp4_edit_list_hides_is_read_with_ignore_editlist(monkeypatch, tmp_path, full, ok):
    """In an M4V, the edit list ends the AAC stream before its last sample, so ffmpeg reads 56,933
    packets, and mkvmerge keeps all 56,934. A second read of the original with -ignore_editlist must match the new file,
    and only then the proof passes."""
    streams = [{"index": 0, "codec_type": "audio", "codec_name": "aac"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("mov,mp4,m4a,3gp,3g2,mj2" if p.endswith(".m4v") else "matroska,webm", streams, 1322.0))
    reads = []

    def hashes(path, maps, bsf, texts, folder, timeout, raw=False, opts=()):
        reads.append((os.path.basename(path), opts))
        if opts:
            return {0: {"count": full[1], "digest": full[0]}}, {}
        a = {"count": 56933, "empty": 0, "digest": "a56933", "start": 0.0, "end": 1321.9} if path.endswith(".m4v") else \
            {"count": 56934, "empty": 0, "digest": "a56934", "but_last": "a56933", "last": 9, "last_pts": 1321.9, "start": 0.0, "end": 1321.93}
        return {0: a}, {}
    monkeypatch.setattr(hook, "packet_hashes", hashes)
    (tmp_path / "Episode.m4v").touch(); (tmp_path / "new.mkv").touch()
    fault, proof = proved(str(tmp_path / "Episode.m4v"), str(tmp_path / "new.mkv"), [], str(tmp_path))
    assert reads[-1] == ("Episode.m4v", ("-ignore_editlist", "1"))
    if ok:
        assert fault is None and proof[0]["edit_list"] == {"pts": 1321.9, "size": 9} and proof[0]["count"] == 56934, proof
    else:
        assert fault == "stream audio 0 (aac) holds 56934 packets in the new file, 56933 in the original", fault


CC_TEXT = ('1\n00:00:04,921 --> 00:00:07,655\n<font face="Monospace">{\\an7}♪ The first line, and you’re\nstill in the first cue ♪</font>\n\n'
           '2\n00:00:07,655 --> 00:00:12,390\n<font face="Monospace">{\\an7}\\h\\h\\h♪ The second cue\nhas two lines too ♪</font>\n\n'
           '3\n00:00:14,202 --> 00:00:16,337\n<font face="Monospace">{\\an7}♪ The third cue ♪\n\\h\\h\\h\\h\\h\\h\\hHey!</font>\n')   # as ffmpeg writes a c608 track


@pytest.mark.parametrize("case", ["text", "empty", "failed"])
def test_cea608_captions_become_a_clean_subrip(monkeypatch, tmp_path, case):
    """An M4V file may carry a c608 caption track, which mkvmerge drops. ffmpeg writes it as SubRip, and the hook
    removes the markup a SubRip player shows as text. No text, or a failed read, keeps the original."""
    def run(argv, **kw):
        with open(argv[-1], "w") as f:
            f.write({"text": CC_TEXT, "empty": "", "failed": ""}[case])
        return types.SimpleNamespace(returncode=1 if case == "failed" else 0, stderr="[in#0 @ 0x5] Error opening input" if case == "failed" else "")
    monkeypatch.setattr(hook.subprocess, "run", run)
    got, why = hook.convert_captions("/m/Episode.m4v", [{"index": 2, "codec_name": "eia_608"}], str(tmp_path), 300e6)
    if case != "text":
        assert got is None and why.startswith("the CEA-608 captions of stream 2 give no text"), why
        assert ("ffmpeg exited 1" in why) == (case == "failed")
        return
    assert why is None and got == {2: {"path": str(tmp_path / "cc2.srt"), "name": "English (CC)", "charset": "UTF-8", "cues": 3}}
    assert (tmp_path / "cc2.srt").read_text() == ("1\n00:00:04,921 --> 00:00:07,655\n♪ The first line, and you’re\nstill in the first cue ♪\n\n"
                                                  "2\n00:00:07,655 --> 00:00:12,390\n♪ The second cue\nhas two lines too ♪\n\n"
                                                  "3\n00:00:14,202 --> 00:00:16,337\n♪ The third cue ♪\nHey!\n")


@pytest.mark.parametrize("new_text, fault", [(None, None), ("1\n00:00:04,921 --> 00:00:07,655\nOther\n", "the captions of stream 2 holds 1 cues")])
def test_the_proof_compares_the_caption_text(monkeypatch, tmp_path, new_text, fault):
    """The c608 stream pairs with the English (CC) track after the kept subtitles, and before the sidecars."""
    src = [{"index": 0, "codec_type": "audio", "codec_name": "aac"}, {"index": 1, "codec_type": "video", "codec_name": "h264"},
           {"index": 2, "codec_type": "subtitle", "codec_name": "eia_608"}, {"index": 3, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}]
    new = [{"index": 0, "codec_type": "audio", "codec_name": "aac"}, {"index": 1, "codec_type": "video", "codec_name": "h264"},
           {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"}]
    monkeypatch.setattr(hook, "ff_streams", lambda p: ("mov,mp4" if p.endswith(".m4v") else "matroska,webm", src if p.endswith(".m4v") else new, 600.0))
    cc = tmp_path / "cc2.srt"
    cc.write_text("1\n00:00:04,921 --> 00:00:07,655\nA line\n\n2\n00:00:09,000 --> 00:00:10,000\nHey!\n")
    same = {"count": 10, "empty": 0, "digest": "d", "start": 0.0, "end": 600.0}

    def hashes(path, maps, bsf, texts, folder, timeout, raw=False):
        return {i: same for i in maps}, {i: new_text or cc.read_text() for i in texts}
    monkeypatch.setattr(hook, "packet_hashes", hashes)
    (tmp_path / "Episode.m4v").touch(); (tmp_path / "new.mkv").touch()
    got, proof = proved(str(tmp_path / "Episode.m4v"), str(tmp_path / "new.mkv"), [], str(tmp_path),
                            {2: {"path": str(cc), "name": "English (CC)", "charset": "UTF-8", "cues": 2}})
    assert (got or "").startswith(fault) if fault else got is None, got
    assert proof[-1]["method"] == "caption text" and proof[-1]["stream"] == "subtitle 2" and proof[-1]["match"] == (fault is None)


def test_the_proof_refuses_a_sidecar_whose_text_changed(convertible, tmp_path):
    out = tmp_path / "new.mkv"
    subs = remux(convertible / "Movie (2020).mp4", out)
    subs[1] = dict(subs[1], path=str(tmp_path / "other.srt"), name="other.srt")
    (tmp_path / "other.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld!\n")
    fault, proof = proved(str(convertible / "Movie (2020).mp4"), str(out), subs, str(tmp_path))
    assert fault == "the sidecar other.srt differs at 0:03: 'World!' against 'World'" and not proof[-2]["match"], fault


def test_a_real_file_is_converted_renamed_and_its_sidecars_go(convertible, tmp_path, monkeypatch, settings):
    """convert() end to end on a copy, with the app's re-link faked: the new .mkv holds the sidecars, and the MP4, the
    sidecars and every temp file are gone. When the app does not take the new file, everything is back as it was."""
    settings(log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    monkeypatch.setattr(hook, "app_lists", lambda app, owner, items: ({}, {}))
    monkeypatch.setattr(hook.Radarr, "extras", lambda self, owner, fid, path, home, items: [])
    monkeypatch.setattr(hook, "score_refuses", lambda app, owner, old, new: None)
    for takes in (False, True):
        folder = tmp_path / str(takes)
        shutil.copytree(convertible, folder, ignore=shutil.ignore_patterns("Clip*", "s.srt", "lead.ass", "Lead.mp4"))
        mp4 = folder / "Movie (2020).mp4"
        before = sorted(os.listdir(folder))
        monkeypatch.setattr(hook.Radarr, "record", lambda self, ids: ({"id": 11, "path": str(mp4)}, {7: True}, str(folder)))
        monkeypatch.setattr(hook, "relink", lambda app, owner, old, items, target, imports=None: (takes or target.endswith(".mp4"),
                                                                                     {"import": "completed", "file_id": 12, "listed": [target], "remonitored": []}))
        monkeypatch.setattr(hook, "app_now", lambda app, owner, items: [str(mp4)])   # the app still lists the original
        _, result, info, now = hook.convert("radarr", str(mp4), hook.mkvmerge(str(mp4)), os.stat(mp4), True, {"app_id": 7, "file_id": 11})
        if not takes:
            assert result.startswith("repack failed: the app did not take the new file") and sorted(os.listdir(folder)) == before, result
            assert info["restored"]["listed"] == [str(mp4)] and now == str(mp4)
            continue
        assert (result, now, os.listdir(folder)) == ("repacked", str(folder / "Movie (2020).mkv"), ["Movie (2020).mkv"]), (result, info)
        assert [t["type"] for t in hook.mkvmerge(now)["tracks"]] == ["video", "audio", "subtitles", "subtitles", "subtitles", "subtitles"]


def real_convert(convertible, tmp_path, monkeypatch):
    """A copy of the real MP4 of convertible under tmp_path, with the app calls of convert() faked as in
    test_a_real_file_is_converted_renamed_and_its_sidecars_go(). Returns its path."""
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path)))
    monkeypatch.setattr(hook.Radarr, "extras", lambda self, owner, fid, path, home, items: [])
    monkeypatch.setattr(hook, "score_refuses", lambda app, owner, old, new: None)
    folder = tmp_path / "Movie (2020)"
    shutil.copytree(convertible, folder, ignore=shutil.ignore_patterns("Clip*", "s.srt", "lead.ass", "Lead.mp4"))
    mp4 = folder / "Movie (2020).mp4"
    monkeypatch.setattr(hook.Radarr, "record", lambda self, ids: ({"id": 11, "path": str(mp4)}, {7: True}, str(folder)))
    return mp4


def test_a_stranded_forced_conversion_removes_the_kept_link_of_the_held_original(convertible, tmp_path, monkeypatch, settings):
    """A forced conversion keeps the original. The app does not take the new file and then does not answer, so the
    original stays at its held name for a person. The held name still holds the original, so its kept link goes."""
    mp4 = real_convert(convertible, tmp_path, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    fault = "stream video 0 (h264) holds 1006 packets in the new file, 1000 in the original"
    monkeypatch.setattr(hook, "prove", lambda *a, **k: ((0, fault), {}))
    monkeypatch.setattr(hook, "relink", lambda app, owner, old, items, target, imports=None: (False, {"import": "failed", "listed": [], "remonitored": []}))
    monkeypatch.setattr(hook, "app_now", lambda app, owner, items: (_ for _ in ()).throw(ConnectionError("the app does not answer")))
    st, held = os.stat(mp4), hook.held_name(str(mp4))
    _, result, info, now = hook.convert("radarr", str(mp4), hook.mkvmerge(str(mp4)), st, True, {"app_id": 7, "file_id": 11},
                                        force=f"repack failed: {fault}")
    assert result.startswith("repack failed, the original and the new file both stay for a person: ") and info["forced"] == fault, result
    assert info["restored"]["result"] == "stranded" and "kept" not in info and now == str(mp4)
    assert os.stat(held).st_ino == st.st_ino and os.stat(held).st_nlink == 1 and kept_files(tmp_path) == []


NEW_PROBE = lambda argv: "-J" in argv and argv[-1].endswith(".repack-tmp")   # mkvmerge -J of a remux's temp file


def sigterm_in_finalizer(monkeypatch, hit, sig=signal.SIGTERM):
    """sig to this process from Popen.__del__ of the first subprocess whose argv hit() takes. A handler that raises
    there raises in a finalizer, and CPython drops the exception. Returns the argv it was sent for."""
    monkeypatch.setitem(hook.STOP, "term", None)
    real_del, sent = subprocess.Popen.__del__, []
    def finalize(self):
        if not sent and hit(self.args):
            sent.append(self.args)
            os.kill(os.getpid(), sig)
        real_del(self)
    monkeypatch.setattr(subprocess.Popen, "__del__", finalize)
    return sent


def test_a_header_repair_remux_runs_with_no_time_limit(mkvs, tmp_path, monkeypatch, settings):
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "Movie (2000)" / "long.mkv"
    path.parent.mkdir()
    shutil.copy(mkvs / "long.mkv", path)
    j = hook.mkvmerge(str(path))
    hp = hook.header_of(str(path), j)
    real, remux = hook.subprocess.run, []
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: ("-o" in argv and remux.append((kw, hook.DEADLINE.end))) or real(argv, **kw))
    hook.DEADLINE.start(hook.BUDGET)
    assert hook.repack(str(path), j, os.stat(path), True, hp)[1] == "header repaired"
    assert remux and all("timeout" not in kw and end is None for kw, end in remux), remux


def test_a_sigterm_that_a_finalizer_drops_still_stops_the_header_repair(mkvs, tmp_path, monkeypatch, settings):
    """The repair renames the temp file over the original with no no_stop() block. check() raises the dropped stop
    before that rename."""
    settings(log=str(tmp_path / "log.jsonl"))
    path = tmp_path / "Movie (2000)" / "long.mkv"
    path.parent.mkdir()
    shutil.copy(mkvs / "long.mkv", path)
    before, j = path.read_bytes(), hook.mkvmerge(str(path))
    hp = hook.header_of(str(path), j)
    sent = sigterm_in_finalizer(monkeypatch, NEW_PROBE)
    with pytest.raises(SystemExit) as got:
        hook.repack(str(path), j, os.stat(path), True, hp)
    assert got.value.code == 128 + signal.SIGTERM and sent and path.read_bytes() == before and os.listdir(path.parent) == ["long.mkv"]


@pytest.mark.parametrize("when", ["remux", "probe"])
def test_a_sigterm_that_a_finalizer_drops_still_stops_the_conversion(convertible, tmp_path, monkeypatch, when):
    """SIGTERM can land while Popen.__del__ runs, after the remux or after the probe of the new file. CPython drops the
    SystemExit that stopped() raises there. sync() raises it again before the proof, or check() before the rename. The
    temp file goes, the original stays, and the stop ends the process."""
    mp4 = real_convert(convertible, tmp_path, monkeypatch)
    before, names = mp4.read_bytes(), sorted(os.listdir(mp4.parent))
    sent = sigterm_in_finalizer(monkeypatch, {"remux": lambda a: "-o" in a, "probe": NEW_PROBE}[when])
    proofs, prove = [], hook.prove
    monkeypatch.setattr(hook, "prove", lambda *a, **k: proofs.append(1) or prove(*a, **k))
    handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as got:
        hook.convert("radarr", str(mp4), hook.mkvmerge(str(mp4)), os.stat(mp4), True, {"app_id": 7, "file_id": 11})
    assert got.value.code == 128 + signal.SIGTERM and sent and mp4.read_bytes() == before and sorted(os.listdir(mp4.parent)) == names
    assert signal.getsignal(signal.SIGTERM) == handler and len(proofs) == (when == "probe")


@pytest.mark.parametrize("when", ["remux", "probe"])
def test_a_ctrl_c_that_a_finalizer_drops_still_stops_the_conversion(convertible, tmp_path, monkeypatch, when):
    """Ctrl+C can land in Popen.__del__ too, and CPython drops its KeyboardInterrupt there. stopped() records it, and
    sync() or check() raises it again before the proof or the rename. The swap puts both handlers back."""
    mp4 = real_convert(convertible, tmp_path, monkeypatch)
    before, names = mp4.read_bytes(), sorted(os.listdir(mp4.parent))
    sent = sigterm_in_finalizer(monkeypatch, {"remux": lambda a: "-o" in a, "probe": NEW_PROBE}[when], signal.SIGINT)
    mine = {signal.SIGTERM: signal.SIG_IGN, signal.SIGINT: signal.default_int_handler}   # the caller's, unlike stopped()
    handlers = {s: signal.signal(s, h) for s, h in mine.items()}
    try:
        with pytest.raises(KeyboardInterrupt):
            hook.convert("radarr", str(mp4), hook.mkvmerge(str(mp4)), os.stat(mp4), True, {"app_id": 7, "file_id": 11})
        back = {s: signal.getsignal(s) for s in mine}
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)
    assert sent and mp4.read_bytes() == before and sorted(os.listdir(mp4.parent)) == names and back == mine


def test_a_ctrl_c_stays_ignored_in_a_process_that_ignores_it(convertible, tmp_path, monkeypatch):
    """A backfill started with & from a script starts with SIGINT ignored. The swap keeps it ignored, so a Ctrl+C in
    the script's terminal never stops the conversion. SIGTERM still raises through stopped()."""
    mp4 = real_convert(convertible, tmp_path, monkeypatch)
    monkeypatch.setattr(hook, "relink", lambda app, owner, old, items, target, imports=None:
                        (True, {"import": "completed", "file_id": 12, "listed": [target], "remonitored": []}))
    seen = []   # the handlers while the remux runs
    sent = sigterm_in_finalizer(monkeypatch, lambda a: "-o" in a and not seen.append([signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)]),
                                signal.SIGINT)
    handlers = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        _, result, info, now = hook.convert("radarr", str(mp4), hook.mkvmerge(str(mp4)), os.stat(mp4), True, {"app_id": 7, "file_id": 11})
        back = [signal.getsignal(s) for s in handlers]
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)
    assert sent and (result, now) == ("repacked", str(mp4.parent / "Movie (2020).mkv")), (result, info)
    assert seen == [[hook.stopped, signal.SIG_IGN]] and back == [signal.SIG_IGN] * 2


def test_a_stop_that_a_finalizer_dropped_ends_the_job_before_its_edit(env, monkeypatch):
    """A job process: job_term() raises in a finalizer during a check, and CPython drops it. The job runs on to its
    edit, and no_stop() raises it again before the editing line. So no write starts after a stop."""
    monkeypatch.setitem(hook.STOP, "held", False)
    monkeypatch.setitem(hook.STOP, "term", None)
    class Finalizer:
        def __del__(self):
            hook.job_term(signal.SIGTERM, None)
    real = hook.check_audio
    def check_audio(*a, **k):
        Finalizer()   # dropped at once, so its __del__ runs here
        return real(*a, **k)
    monkeypatch.setattr(hook, "check_audio", check_audio)
    with pytest.raises(SystemExit) as got:
        hook.main([])
    assert got.value.code == 128 + signal.SIGTERM and env["mkvpropedit"] == [] and not os.path.exists(hook.CFG.log)   # no editing line


def test_a_sigterm_during_the_relink_lets_the_job_finish(convertible, tmp_path, monkeypatch):
    """The swap puts the job's SIGTERM handler back before no_stop(). A SIGTERM that waited for the block then reaches
    job_term(), and the conversion ends with its result. With the remux handler it would stop the job there."""
    mp4 = real_convert(convertible, tmp_path, monkeypatch)
    monkeypatch.setitem(hook.STOP, "held", False)
    monkeypatch.setitem(hook.STOP, "asked", False)

    def relink(app, owner, old, items, target, imports=None):
        os.kill(os.getpid(), signal.SIGTERM)   # blocked here, delivered when no_stop() ends
        return True, {"import": "completed", "file_id": 12, "listed": [target], "remonitored": []}
    monkeypatch.setattr(hook, "relink", relink)
    old = signal.signal(signal.SIGTERM, hook.job_term)
    try:
        _, result, info, now = hook.convert("radarr", str(mp4), hook.mkvmerge(str(mp4)), os.stat(mp4), True, {"app_id": 7, "file_id": 11})
    finally:
        signal.signal(signal.SIGTERM, old)
    assert (result, now) == ("repacked", str(mp4.parent / "Movie (2020).mkv")) and hook.STOP["asked"] is True, (result, info)


def test_an_empty_plex_url_makes_no_plex_call(env, monkeypatch, settings):
    """With PLEX_URL empty there is no Plex. The edit still happens, no request goes to Plex, no Plex line is logged,
    and --plex-flush stops with a message."""
    settings(plex_url="")
    hook.main([])
    assert [r["outcome"] for r in log_lines(env) if r.get("outcome")] == ["edited"]
    assert not [u for m, u, b in env["http"] if "/library/" in u or "/activities" in u] and plex_lines(env) == []
    with pytest.raises(SystemExit, match="PLEX_URL is empty"):
        hook.plex_flush(["radarr"])


# --- subtitle text language (docs/design.md, "Subtitle text") -----------------------------------------------

SUBS = os.path.join(os.path.dirname(__file__), "fixtures", "subtitles")


def cue_texts(name, times=3):
    """The cue texts of a SubRip file of the corpus, parsed as sidecar_subs() parses a sidecar. A scene holds about a
    third of the TEXT_STOP letters a real track gives, so it plays three times by default."""
    with open(os.path.join(SUBS, f"{name}.srt"), "rb") as f:
        return [c[2] for c in hook.srt_cues(f.read().decode("utf-8-sig"))] * times


# One scene of original dialogue in each language, as real-shape SubRip. A name that ends in _plain drops the accents,
# as some fansubs type. English with some Spanish words stays English. Catalan, Croatian and Serbian have no list, and
# they and mixed text get no answer.
CORPUS = {"eng": "eng", "spa": "spa", "spa_plain": "spa", "por": "por", "por_plain": "por", "fre": "fre", "ger": "ger", "ita": "ita",
          "rum": "rum", "rum_plain": "rum", "dut": "dut", "afr": "afr", "swe": "swe", "dan": "dan", "nor": "nor", "pol": "pol", "cze": "cze",
          "slo": "slo", "tur": "tur", "rus": "rus", "srp": None,
          "ukr": "ukr", "gre": "gre", "ara": "ara", "per": "per", "chi": "chi", "jpn": "jpn", "kor": "kor",
          "eng_spanish_words": "eng", "cat": None, "hrv": None, "mixed_eng_spa": None, "mixed_chi_eng": None}


def test_the_corpus_table_lists_every_corpus_file():
    assert sorted(CORPUS) == sorted(n[:-4] for n in os.listdir(SUBS))


@pytest.mark.parametrize("name, lang", CORPUS.items())
def test_the_text_language_of_each_corpus_file(name, lang):
    got = hook.arr_decide.text_language(cue_texts(name))
    assert got[0] == lang, got
    assert got[1] >= hook.arr_decide.TEXT_SHARE if lang else got[1] < hook.arr_decide.TEXT_SHARE, got


@pytest.mark.parametrize("name, why", [("mixed_eng_spa", "mixed, eng holds"), ("mixed_chi_eng", "mixed scripts"), ("cat", "telling words"),
                                       ("hrv", "telling words")])
def test_mixed_text_and_a_language_with_no_list_say_why_they_get_no_answer(name, why):
    assert why in hook.arr_decide.text_language(cue_texts(name))[2]


def test_short_text_gets_no_answer():
    """Four cues, or a signs track, hold too few letters to judge."""
    assert hook.arr_decide.text_language(cue_texts("eng", 1)[:4])[:2] == (None, 0.0)
    assert hook.arr_decide.text_language(["[DOOR SLAMS]", "EXIT", "Sign: Main Street"])[2].startswith("short")


def test_the_count_stops_at_its_first_verdict():
    """The cues are read only as far as the verdict needs, so a Matroska read stops early too. Clear English stops at
    the first count. Mixed text stops at TEXT_STOP."""
    d = hook.arr_decide
    for name, most in (("eng", d.TEXT_STEP), ("mixed_eng_spa", d.TEXT_STOP)):
        seen = []
        def cues():
            for t in cue_texts(name, 20):
                seen.append(t)
                yield t
        d.text_language(cues())
        assert sum(c.isalpha() for t in seen[:-1] for c in t) < most, name


def test_a_few_early_words_of_another_language_do_not_decide():
    """Two English cues before Romanian text give a share between TEXT_MIXED and TEXT_SHARE at the first counts. The
    count reads on, and the Romanian text decides."""
    early = ["Or maybe the other one, or this one?", "What about the one with the red door?"]
    assert hook.arr_decide.text_language(early + cue_texts("rum"))[0] == "rum"


def test_serbian_letters_rule_out_russian_and_ukrainian():
    """Serbian and Macedonian share most Russian stopwords. One of their own letters is enough to refuse rus and ukr."""
    d = hook.arr_decide
    assert d.text_language(cue_texts("srp"))[2] == "Serbian or Macedonian letters rule out rus"
    assert d.text_language(cue_texts("rus"))[0] == "rus"
    assert d.text_language(["Његош је рекао."] + cue_texts("rus"))[0] is None
    assert d.text_language(["Сѕвезда."] + cue_texts("ukr"))[2] == "Serbian or Macedonian letters rule out ukr"


def test_an_answer_needs_enough_telling_words():
    """Twenty Spanish cues hold 17 telling words, under TEXT_TELL."""
    assert hook.arr_decide.text_language(cue_texts("spa", 1)[:20]) == (None, 0.0, "17 telling words")


def test_a_text_mostly_outside_every_list_gets_no_answer():
    """A text whose list words are under TEXT_COVER gets no answer, even when every telling word is English. A language
    with no list can hit one list's words now and then, and this check stops that. Here "the" is one word in six."""
    nouns = "harbor lantern midnight velvet copper orchard falcon meadow thunder canyon silver prairie".split()
    cues = [" ".join(["The"] + [nouns[(i + k) % 12] for k in (0, 3, 5, 7, 9)]) for i in range(60)]
    assert hook.arr_decide.text_language(cues) == (None, 1.0, "no list fits, eng words are 17% of the text")


def test_clearly_mixed_text_stops_at_once():
    """Two Spanish cues to each English one give a share under TEXT_MIXED, so the count stops there. English cues
    that follow are never read."""
    eng, spa = cue_texts("eng", 1), cue_texts("spa", 1)
    mixed = [x for i in range(12) for x in (spa[2 * i], spa[2 * i + 1], eng[i])]
    got = hook.arr_decide.text_language(mixed + eng * 6)
    assert got[0] is None and got[2].startswith("mixed, eng holds 60%"), got


def test_the_lists_read_cedillas_and_yo_as_they_spell_them():
    """Old Romanian subtitles write ş and ţ with a cedilla, and Russian ones often write ё. Each word here matches a
    list only after the fold."""
    d = hook.arr_decide
    assert d.text_language(["Ştiu. Şi eşti? Poţi."] * 30)[0] == "rum"
    assert d.text_language(["Её? Ещё её."] * 60)[0] == "rus"


def test_tags_and_ass_override_blocks_are_no_words():
    """{\be1} would read as the English word "be" in every cue and make Spanish text look mixed."""
    assert hook.arr_decide.text_language(["{\\be1}<i>" + t + "</i>" for t in cue_texts("spa")])[0] == "spa"


def test_romanian_cedillas_read_as_romanian():
    """Old Romanian subtitles write ş and ţ with a cedilla. The lists spell them with a comma below."""
    texts = [t.replace("ș", "ş").replace("ț", "ţ") for t in cue_texts("rum")]
    assert hook.arr_decide.text_language(texts)[0] == "rum"


def test_a_near_miss_never_takes_the_other_language():
    """English with some Spanish words, Portuguese against Spanish, and the close Nordic and West Slavic languages keep
    their language or get no answer."""
    assert hook.arr_decide.text_language(cue_texts("eng_spanish_words"))[0] in (None, "eng")
    for name, other in (("por", "spa"), ("por_plain", "spa"), ("spa", "por"), ("spa_plain", "por"), ("dan", "nor"), ("nor", "dan"), ("swe", "dan"),
                        ("cze", "pol"), ("slo", "pol"), ("cze", "slo"), ("afr", "dut"), ("dut", "afr"), ("srp", "rus")):
        assert hook.arr_decide.text_language(cue_texts(name))[0] != other, name


def test_the_read_language_is_one_signal_of_the_retag_rule():
    """A lone mismatch keeps the tag and alerts. With the title it makes two signals and the tag changes. A tag
    text_language() cannot name never alerts."""
    d, table = hook.arr_decide, hook.langs()
    sub = lambda lang, **kw: tracks(("audio", "eng", None, 1, True, {"audio_channels": 2}), ("subtitles", lang, None, 2, True, kw))
    r = d.retag(sub("eng"), read={"s1": "rum"}, table=table)
    assert (r["edits"], r["set"], r["mismatch"]) == ([], {}, ["subtitle track 1 is tagged English, but its text reads as Romanian"]), r
    assert "subtitle_text_mismatch" in r["reasons"] and "s1 keeps eng: eng (tagged eng); rum (the text reads rum)" in r["notes"]
    titled = sub("eng", track_name="French")
    assert d.retag(titled, table=table)["to_read"] == {"s1"} and not d.retag(sub("eng"), table=table)["to_read"]
    r = d.retag(titled, read={"s1": "fre"}, table=table)
    assert (r["edits"], r["set"], r["mismatch"]) == ([["track:=2", "fr", "eng", "language"], ["track:=2", "fr", None, "language-ietf"]],
                                                     {"s1": "fre"}, []), r
    assert d.retag(titled, read={"s1": "eng"}, table=table)["edits"] == []   # the tag and the text beat the title
    r = d.retag(sub("und", track_name="English"), read={"s1": "eng"}, table=table)
    assert r["edits"][0] == ["track:=2", "en", "und", "language"] and r["set"] == {"s1": "eng"}
    assert d.retag(sub("hrv"), read={"s1": "pol"}, table=table)["mismatch"] == []
    assert d.retag(sub("eng"), read={"s1": "eng"}, table=table)["mismatch"] == []
    assert d.retag(sub("eng"), read={"s1": "rum"}, table=table)["wrong"] == {"s1": "rum"}
    assert [t["lang"] for t in d.decide(sub("eng"), "English", heard={"s1": "fre"})["tracks"]] == ["eng", "fre"]   # retag's set reaches decide()


def test_an_und_subtitle_takes_its_read_language_alone():
    """An und tag claims no language, so a sure read sets it, and the flag rules then use the new language. A title or
    a BCP 47 tag that names another language is a claim, and the tag stays und. Short text sets nothing."""
    d, table = hook.arr_decide, hook.langs()
    und = lambda **kw: tracks(("audio", "eng", None, 1, True, {"audio_channels": 2}), ("subtitles", "und", None, 2, True, kw))
    assert d.retag(und(), table=table)["to_read"] == {"s1"}
    r = d.retag(und(), read={"s1": "rum"}, table=table)
    assert (r["edits"][0], r["set"], r["mismatch"]) == (["track:=2", "rum", "und", "language"], {"s1": "rum"}, []), r
    assert r["notes"] == ["s1 und -> rum: the text reads rum, and the und tag names no language"] and "language_tag_set" in r["reasons"]
    assert d.decide(und(), "English", heard=r["set"])["edits"] == [["track:=2", 0, 1]]   # a Romanian default under English audio goes off
    assert d.retag(und(track_name="French"), read={"s1": "eng"}, table=table)["edits"] == []   # the title is a claim: a tie
    assert d.retag(und(language_ietf="fr"), read={"s1": "eng"}, table=table)["edits"] == []   # so is a BCP 47 tag
    assert d.retag(und(), read={}, table=table)["edits"] == []   # short or mixed text reads as nothing


def last_forced(j, forced=True):
    """j with the forced flag of its last track set, which mk() writes as False."""
    j["tracks"][-1]["properties"]["forced_track"] = forced
    return j


def test_a_wrong_language_no_audio_speaks_loses_its_flags():
    """Rather no subtitle than a wrong one. A read language that contradicts the tag, with nothing to back a new tag,
    takes the default and forced flags when no main audio track speaks it. The tag stays."""
    d = hook.arr_decide
    subs = lambda *audio, forced=True: last_forced(tracks(*[("audio", a, None, i + 1, i == 0, {"audio_channels": 2}) for i, a in enumerate(audio)],
                                                         ("subtitles", "eng", None, 9, True, {})), forced)
    p = d.decide(subs("eng"), "English", wrong={"s1": "rum"})
    assert p["edits"] == [["track:=9", 0, 1], ["track:=9", 0, 1, d.FORCED_FLAG]] and not p["dropped"], p
    assert "subtitle_text_muted" in p["reasons"] and p["tracks"][1]["tag"] == "eng" and p["tracks"][1]["lang"] == "rum"
    # a foreign film with no other English subtitle: without the rule the Romanian text would stay on as the English subtitle
    p = d.decide(subs("jpn", forced=False), "Japanese", wrong={"s1": "rum"})
    assert p["edits"] == [["track:=9", 0, 1]] and not p["dropped"], p
    assert d.decide(subs("jpn", forced=False), "Japanese")["edits"] == []


def test_a_muted_track_counts_as_its_read_language():
    """A French film: the default English subtitle holds Romanian text, and an English SDH track is off. The muted
    track counts as Romanian, so the normal rules give the default to the real English track."""
    d = hook.arr_decide
    j = tracks(("audio", "fre", None, 1, True, {"audio_channels": 6}), ("subtitles", "eng", None, 2, True, {}),
               ("subtitles", "eng", None, 3, False, {"track_name": "SDH"}))
    assert d.decide(j, "French")["edits"] == []
    p = d.decide(j, "French", wrong={"s1": "rum"})
    assert p["edits"] == [["track:=2", 0, 1], ["track:=3", 1, 0]] and p["rules"] == ["foreign subtitle off", "sdh English subtitle on"], p


def test_a_wrong_language_that_an_audio_track_speaks_keeps_its_flags():
    """Near misses: forced English text tagged French on English audio, and Romanian text beside a Romanian audio track.
    The new rule changes nothing there."""
    d = hook.arr_decide
    fre = last_forced(tracks(("audio", "eng", None, 1, True, {"audio_channels": 2}), ("subtitles", "fre", None, 9, True, {})))
    assert d.decide(fre, "English", wrong={"s1": "eng"})["edits"] == d.decide(fre, "English")["edits"]
    two = last_forced(tracks(("audio", "eng", None, 1, True, {"audio_channels": 2}), ("audio", "rum", None, 2, False, {"audio_channels": 2}),
                             ("subtitles", "eng", None, 9, True, {})))
    p = d.decide(two, "English", wrong={"s1": "rum"})
    assert p["edits"] == d.decide(two, "English")["edits"] and "subtitle_text_muted" not in p["reasons"], p


def test_the_sidecar_rule():
    d = hook.arr_decide
    rum, eng, none = ("rum", 1.0, "100% rum"), ("eng", 1.0, "100% eng"), (None, 0.0, "short")
    assert d.sidecar_language("eng", rum, {"eng"}) == ("rum", False, "named eng, but the text reads as rum: 100% rum")
    assert d.sidecar_language("eng", rum, {"eng", "rum"})[:2] == ("rum", True)   # Romanian audio: a forced Romanian track is right
    assert d.sidecar_language("eng", eng, {"eng"}) is None and d.sidecar_language("eng", none, {"eng"}) is None
    assert d.sidecar_language(None, rum, {"eng"}) is None   # no language in the name
    assert d.sidecar_language("hrv", ("pol", 0.95, ""), {"eng"}) is None   # the lists cannot name Croatian, so the name stays


def test_a_sidecar_whose_text_reads_another_language_is_muxed_with_it(env, monkeypatch):
    """Romanian text named .en.forced.srt must never become an English forced track. It goes in as Romanian, with no
    forced flag, because the audio is English. The English sidecar keeps its name's language."""
    mp4, mkv = mp4_import(env, monkeypatch)
    for ext, name in ((".en.srt", "eng"), (".en.forced.srt", "rum")):
        shutil.copy(os.path.join(SUBS, f"{name}.srt"), mkv[:-4] + ext)
    hook.main([])
    rec = decided(env)
    remux = env["repacks"][0]
    forced = remux.index(mkv[:-4] + ".en.forced.srt")
    assert remux[forced - 6:forced] == ["--language", "0:rum", "--sub-charset", "0:UTF-8", "--default-track-flag", "0:0"], remux
    assert [(s["lang"], s["flags"], s["read"]) for s in rec["repack"]["sidecars"]] == [("rum", [], "rum"), ("en", [], "eng")], rec["repack"]
    assert rec["repack"]["sidecars"][0]["mismatch"].startswith("named eng, but the text reads as rum: ") and "mismatch" not in rec["repack"]["sidecars"][1]


def test_near_miss_sidecars_keep_their_names(env, monkeypatch):
    """Portuguese named pt, Spanish named es, and English with Spanish words named en all keep their language."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=(".pt.srt", ".es.srt", ".en.srt"))
    for ext, name in ((".pt.srt", "por"), (".es.srt", "spa"), (".en.srt", "eng_spanish_words")):
        shutil.copy(os.path.join(SUBS, f"{name}.srt"), mkv[:-4] + ext)
    hook.main([])
    assert [(s["lang"], s.get("mismatch")) for s in decided(env)["repack"]["sidecars"]] == [("en", None), ("es", None), ("pt", None)]


SHOWN_SUBS = tracks(("video", "und", None, 1, True, {}), ("audio", "eng", None, 2, True, {"audio_channels": 6}),
                    ("subtitles", "eng", None, 3, True, {"codec_id": "S_TEXT/UTF8"}),     # forced below: it shows by itself
                    ("subtitles", "eng", None, 4, False, {"codec_id": "S_TEXT/UTF8"}),    # shows only when a viewer picks it
                    ("subtitles", "eng", None, 5, False, {"track_name": "French", "codec_id": "S_TEXT/UTF8"}))   # tag and title disagree
SHOWN_SUBS["tracks"][2]["properties"]["forced_track"] = True


def test_the_hook_reads_the_subtitles_a_decision_depends_on(env, monkeypatch):
    """The forced default track and the track whose title questions its tag are read. The third track is not. The
    Romanian text of the forced track has no second signal, so it only alerts. The French text agrees with the title,
    so that tag changes in the flag edit, and the check after the edit plans nothing more."""
    env["probe"] = copy.deepcopy(SHOWN_SUBS)
    asked = []
    monkeypatch.setattr(hook, "subtitle_read", lambda path, j, want: asked.append(set(want)) or
                        {"s1": ("rum", 1.0, "100% rum"), "s3": ("fre", 0.96, "96% fre")})
    hook.main([])
    rec = decided(env)
    assert asked == [{"s1", "s3"}] and rec["read"]["s1"] == {"lang": "rum", "conf": 1.0, "why": "100% rum"}
    assert rec["edits"] == [["track:=3", 0, 1], ["track:=3", 0, 1, "flag-forced"],   # Romanian text no audio speaks goes off, tag and all
                            ["track:=5", "fr", "eng", "language"], ["track:=5", "fr", None, "language-ietf"]], rec
    assert rec["recheck"]["edits"] == 0 and rec["tracks"][3]["lang"] == "fre", rec
    assert "subtitle_text_mismatch" in rec["reasons"] and "subtitle_text_muted" in rec["reasons"] and "sublang" in rec["alert_kinds"]
    text = rec["alerts"][rec["alert_kinds"].index("sublang")]
    assert "Subtitle track 1 is tagged English, but its text reads as Romanian. Nothing else confirms another language, so the tag was kept. " \
           "Turned off its default" in text, text
    p = env["files"][env["path"]]["tracks"][2]["properties"]
    assert (p["language"], p["default_track"], p["forced_track"]) == ("eng", 0, 0), p


def test_the_hook_tags_an_und_subtitle_by_its_text(env, monkeypatch):
    """An und default subtitle reads as Romanian. The tag changes, and the default goes off under English audio."""
    env["probe"] = tracks(("video", "und", None, 1, True, {}), ("audio", "eng", None, 2, True, {"audio_channels": 6}),
                          ("subtitles", "und", None, 3, True, {"codec_id": "S_TEXT/UTF8"}))
    monkeypatch.setattr(hook, "subtitle_read", lambda path, j, want: {"s1": ("rum", 0.97, "97% rum")} if "s1" in want else {})
    hook.main([])
    rec = decided(env)
    assert rec["edits"][:2] == [["track:=3", 0, 1], ["track:=3", "rum", "und", "language"]] and rec["recheck"]["edits"] == 0, rec
    assert "s1 und -> rum: the text reads rum, and the und tag names no language" in rec["notes"] and "sublang" not in rec.get("alert_kinds", [])


def test_the_syslog_line_says_which_tmdb_answer_a_file_got(env):
    """found, no_record or a failure code, and not_asked when the run asked TMDB nothing, as in a conversion backfill."""
    hook.decision({"app": "radarr", "source": "backfill", "result": "repacked", "outcome": "repacked", "label": "Film A"}, time.time())
    hook.decision({"app": "radarr", "source": "hook", "result": "edited", "outcome": "edited", "label": "Film A", "tmdb": "found"}, time.time())
    assert " tmdb=not_asked " in env["syslog"][-2] and " tmdb=found " in env["syslog"][-1], env["syslog"]


def test_a_read_subtitle_language_is_never_heard_again_on_the_second_check(env, monkeypatch, settings):
    """retag()'s set carries a retagged subtitle into heard, and heard goes to the second check before a re-grab. That
    check hears only audio, so the subtitle must not count as "not heard again" and take out the language point."""
    wrong_film(env, monkeypatch)
    settings(regrab={"audio", "video", "content"})
    real = hook.arr_decide.retag
    monkeypatch.setattr(hook.arr_decide, "retag", lambda j, *a, **k: dict(real(j, *a, **k), set={"s1": "fre"}))
    hook.main([])
    assert decided(env)["outcome"] == "wrong_content"


@pytest.fixture(scope="module")
def text_mkv(tmp_path_factory):
    """Matroska files with five text subtitle tracks: Romanian text tagged eng and forced, French titled "French",
    English, Spanish with zlib compression, and Japanese ASS. mkvmerge writes one, ffmpeg copies it into the other.
    A third has no cue entries for its subtitle."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d = tmp_path_factory.mktemp("text")
    sub = lambda n: os.path.join(SUBS, f"{n}.srt")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=600:size=320x180:rate=25", "-c:v", "libx264",
                    "-preset", "ultrafast", "-threads", "1", str(d / "v.mp4")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", sub("jpn"), str(d / "jpn.ass")], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(d / "mkvmerge.mkv"), str(d / "v.mp4"), "--language", "0:eng", "--forced-display-flag", "0:1", sub("rum"),
                    "--language", "0:eng", "--track-name", "0:French", sub("fre"), "--language", "0:eng", sub("eng"),
                    "--language", "0:spa", "--compression", "0:zlib", sub("spa"), "--language", "0:jpn", str(d / "jpn.ass")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(d / "mkvmerge.mkv"), "-map", "0", "-c", "copy", str(d / "ffmpeg.mkv")], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(d / "nocues.mkv"), str(d / "v.mp4"), "--cues", "0:none", sub("eng")], check=True)
    return d


@pytest.mark.parametrize("name", ["mkvmerge.mkv", "ffmpeg.mkv"])
def test_subtitle_read_takes_each_text_by_its_cue_entry(text_mkv, name):
    """Each track reads right, zlib and ASS included, and the read takes a small part of the file."""
    path = str(text_mkv / name)
    j = REAL_MKVMERGE(path)
    before = hook.rchar()
    got = hook.subtitle_read(path, j, {"s1", "s2", "s3", "s4", "s5"})
    read = hook.rchar() - before
    assert {p: g[0] for p, g in got.items()} == {"s1": "rum", "s2": "fre", "s3": "eng", "s4": "spa", "s5": "jpn"}, got
    assert read < os.path.getsize(path) / 10, (read, os.path.getsize(path))
    assert hook.subtitle_read(path, j, {"s3"}).keys() == {"s3"}   # a track no decision depends on is never read


def test_the_time_limit_ends_a_long_subtitle_read(text_mkv, monkeypatch):
    """A track takes up to CUE_MAX small reads, each a wait on a cold NFS file. The job's time limit ends the read
    between two blocks, and the read never takes OutOfTime for a failed read."""
    path = str(text_mkv / "mkvmerge.mkv")
    j = REAL_MKVMERGE(path)
    clock = [1000.0]
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 1) or clock[0])   # each look takes a second
    hook.DEADLINE.start(20)
    with pytest.raises(hook.arr_meta.OutOfTime):
        hook.subtitle_cues(path, j, {"s3"})
    assert len(hook.subtitle_cues(path, j, {"s3"})["s3"]) == 36   # with no limit it reads every cue


def test_subtitle_read_skips_a_content_encoding_it_cannot_undo(text_mkv):
    """Only zlib is undone. Another content encoding, such as header removal, leaves the track unread."""
    path = str(text_mkv / "mkvmerge.mkv")
    j = REAL_MKVMERGE(path)
    j["tracks"][3]["properties"]["content_encoding_algorithms"] = "3"   # s3, the English text
    assert hook.subtitle_read(path, j, {"s3"}) == {}


def test_a_failed_read_keeps_what_it_read_and_never_stops_the_job(text_mkv):
    """The probe says s3 is zlib-compressed, but its blocks are plain text, so zlib fails. s2 keeps its answer, and s3
    gets no answer with the error."""
    path = str(text_mkv / "mkvmerge.mkv")
    j = REAL_MKVMERGE(path)
    j["tracks"][3]["properties"]["content_encoding_algorithms"] = "0"
    got = hook.subtitle_read(path, j, {"s2", "s3"})
    assert got["s2"][0] == "fre" and got["s3"][:2] == (None, 0.0) and got["s3"][2].startswith("the read failed: error: "), got


def test_a_cue_entry_that_points_at_another_track_reads_nothing(text_mkv, monkeypatch):
    """Every cue entry of s3 points at a video block. block_frame() checks the track number, so no video bytes read as
    text."""
    path = str(text_mkv / "mkvmerge.mkv")
    real = hook.arr_decide.cue_blocks
    monkeypatch.setattr(hook.arr_decide, "cue_blocks", lambda b, tracks, cap: {n: real(b, {1}, cap)[1] for n in tracks})
    assert hook.subtitle_read(path, REAL_MKVMERGE(path), {"s3"}) == {"s3": (None, 0.0, "short, 0 letters")}


def cue_el(i, data):
    """One EBML element with a one-byte size."""
    return i.to_bytes((i.bit_length() + 7) // 8, "big") + bytes([0x80 | len(data)]) + data


def test_cue_blocks_takes_only_entries_with_a_relative_position():
    """The byte search needs a CueRelativePosition right after the CueClusterPosition. An entry where another element
    comes there, a CueDuration here, is left out, so its value never reads as a position. A CueDuration after the
    CueRelativePosition gives the block's duration."""
    d = hook.arr_decide
    point = lambda rest: cue_el(d.CUEPOINT, cue_el(d.CUETIME, b"\x00") + cue_el(d.CUETRACKPOS, cue_el(d.CUETRACK, b"\x02") + cue_el(0xF1, b"\x05") + rest))
    b = point(cue_el(0xF0, b"\x03")) + point(cue_el(0xB2, b"\x07")) + point(cue_el(0xF0, b"\x09") + cue_el(0xB2, b"\x8a"))
    assert d.cue_blocks(b, {2}, 10) == {2: [(5, 3, None), (5, 9, 138)]} and d.cue_blocks(b, {2}, 1) == {2: [(5, 3, None)]} and d.cue_blocks(b, {3}, 10) == {}


def test_block_frame_checks_the_track_and_the_lacing():
    d = hook.arr_decide
    block = lambda track, flags: cue_el(d.SIMPLEBLOCK, bytes([0x80 | track]) + b"\x00\x10" + bytes([flags]) + b"Hello")
    assert d.block_frame(block(2, 0x80), 2) == b"Hello" and d.block_frame(cue_el(d.BLOCKGROUP, cue_el(d.BLOCK, block(2, 0)[2:])), 2) == b"Hello"
    assert d.block_frame(block(3, 0x80), 2) is None   # another track
    assert d.block_frame(block(2, 0x82), 2) is None   # a laced block holds several frames


def test_subtitle_read_skips_what_it_cannot_read(text_mkv):
    """A track the Cues do not index gets no answer, a picture track is never read, and a file that is not Matroska
    gives nothing."""
    path = str(text_mkv / "nocues.mkv")
    assert hook.subtitle_read(path, REAL_MKVMERGE(path), {"s1"}) == {"s1": (None, 0.0, "the Cues index none of its blocks")}
    pgs = {"tracks": [{"type": "subtitles", "properties": {"codec_id": "S_HDMV/PGS", "number": 2}}]}
    assert hook.subtitle_read("/nonexistent.mkv", pgs, {"s1"}) == {}
    assert hook.subtitle_read(str(text_mkv / "v.mp4"), REAL_MKVMERGE(path), {"s1"}) == {}


# --- the subtitle match check (docs/design.md, "Subtitle match") ------------------------------------------------------

import test_arr_subsync as talk  # noqa: E402  the dialogue written for the tests, and the fake hearing


def sub_probe(*subs, audio=(("jpn", True), ("eng", False))):
    """A film of talk.DURATION with the audio tracks audio, (tag, default), and the subtitles subs, (tag, default, more
    properties). A subtitle is SubRip unless it names another codec_id."""
    ts = [("video", "und", None, 1, True, {})] + [("audio", a, None, 2 + i, d, {"audio_channels": 2}) for i, (a, d) in enumerate(audio)]
    ts += [("subtitles", lang, None, 10 + i, d, {"codec_id": "S_TEXT/UTF8", **{k: v for k, v in kw.items() if k != "forced_track"}})
           for i, (lang, d, kw) in enumerate(subs)]
    j = tracks(*ts)
    for t, (_, _, kw) in zip(j["tracks"][len(audio) + 1:], subs):
        t["properties"]["forced_track"] = kw.get("forced_track", False)
    j["container"]["properties"]["duration"] = int(talk.DURATION * 1e9)
    for i, t in enumerate(j["tracks"]):
        t["id"] = i
    return j


def hearing(env, monkeypatch, cues, lines=talk.RIGHT, answer=None, spoken=lambda i: True):
    """The check through its real code, with the model faked: subtitle_cues() reads cues {position: cues}, and a hearing
    hears lines in the windows asked, or gives answer. spoken(i) False drops line i, for a window with little speech.
    env["words"] records (audio index, language, windows) per hearing."""
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False, stop=None: {p: c for p, c in cues.items() if p in want})

    def fake_lid_run(path, index, j, expect, timeout, fresh=False, keep=False, words=None, then=None, yield_to=None):
        if not words:
            return {"why": "no language hearing in these tests"}
        env.setdefault("words", []).append((index, words[0], list(words[1])))
        env.setdefault("group", []).extend(words[4:5])
        if answer:
            return answer
        got = [dict(w, secs=words[2]) for w in talk.heard(words[1], lines, secs=words[2], keep=spoken)]
        few = hook.arr_subsync.short(got, words[0]) if len(words) > 3 and words[3] else []
        extra = [words[3][k] for k in few if words[3][k] is not None] if len(few) < len(got) or len(got) == 1 else []
        env.setdefault("more", []).append(extra)
        return {"windows": got + [dict(w, secs=hook.arr_subsync.THIRD) for w in talk.heard(extra, lines, secs=hook.arr_subsync.THIRD, keep=spoken)],
                "cached": False, "reused": 0, "cpu": 12.5,
                "took": 12.0}
    monkeypatch.setattr(hook, "lid_run", fake_lid_run)


def japanese_film(env, *subs):
    """A Japanese film with Japanese and English audio. A mismatch there is only unknown, see sub_hold()."""
    env["movies"]["movie/7"].update(originalLanguage={"name": "Japanese"}, runtime=round(talk.DURATION / 60))
    env["probe"] = sub_probe(*subs)


def english_film(env, *subs):
    """An English film with English audio only, where a mismatch counts."""
    env["movies"]["movie/7"]["runtime"] = round(talk.DURATION / 60)
    env["probe"] = sub_probe(*subs, audio=(("eng", True),))


def test_sub_targets_take_the_text_tracks_a_viewer_would_use():
    """Full, SDH and dub text tracks in an audio language. A forced or commentary track, a picture track and a track in
    no audio language are never checked."""
    j = sub_probe(("eng", False, {}), ("eng", True, {"forced_track": True, "track_name": "Forced"}), ("fre", False, {}),
                  ("eng", False, {"codec_id": "S_HDMV/PGS"}), ("eng", False, {"codec_id": "S_TEXT/ASS", "track_name": "SDH"}),
                  ("eng", False, {"codec_id": "S_TEXT/WEBVTT"}), ("eng", False, {"track_name": "Commentary"}),
                  audio=(("eng", True),))
    d = hook.arr_decide.decide(j, "English")
    assert hook.sub_targets(j, d) == {"s1": ("eng", 0), "s5": ("eng", 0), "s6": ("eng", 0)}
    # Japanese text has no spaces, so a Japanese track under Japanese audio is never heard
    j = sub_probe(("jpn", False, {}), ("eng", False, {}), audio=(("jpn", True), ("eng", False)))
    assert hook.sub_targets(j, hook.arr_decide.decide(j, "Japanese")) == {"s2": ("eng", 1)}
    # the audio that plays is the one heard when two main tracks speak the language
    j = sub_probe(("eng", False, {}), audio=(("eng", False), ("eng", True)))
    assert hook.sub_targets(j, hook.arr_decide.decide(j, "English")) == {"s1": ("eng", 1)}


def test_a_subtitle_of_another_episode_loses_its_flags_and_alerts(env, monkeypatch):
    """An English film with the English subtitle on. That subtitle holds another episode's lines. With no original
    kept (KEEP_ORIGINALS_DAYS 0) it stays in the file and loses its flags. One hearing serves both tracks."""
    english_film(env, ("eng", True, {}), ("eng", False, {"track_name": "SDH"}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER), "s2": talk.cues(talk.RIGHT)})
    hook.main([])
    rec = decided(env)
    assert [rec["subcheck"][p]["verdict"] for p in ("s1", "s2")] == ["mismatch", "match"], rec["subcheck"]
    assert rec["edits"] == [["track:=10", 0, 1]] and rec["outcome"] == "edited", rec["edits"]
    assert "subtitle_audio_mismatch" in rec["reasons"] and rec["alert_kinds"] == ["submatch"], rec
    line = rec["findings"][0]["lines"][0]
    assert (line["code"], line["track"], line["kept_back"], line["flags_off"]) == ("stays", "s1", "keep_days", True), line
    assert line["why"].startswith("the heard words match the cues at "), line
    assert [(i, lang) for i, lang, _ in env["words"]] == [(0, "eng")]   # the English audio, heard once for both tracks
    assert rec["recheck"]["edits"] == 0   # the re-plan after the edit keeps the track off


def test_a_matching_subtitle_keeps_its_flags(env, monkeypatch):
    japanese_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    hook.main([])
    rec = decided(env)
    assert rec["subcheck"]["s1"]["verdict"] == "match" and rec["subcheck"]["s1"]["timing"]["why"] == "in time"
    assert rec["outcome"] == "no_change" and env["mkvpropedit"] == [] and "alert_kinds" in rec and rec["alert_kinds"] == []


def test_a_file_with_no_track_to_check_is_never_heard(env, monkeypatch):
    """Cost: a forced track, a picture track and a French track under English and Japanese audio need no hearing."""
    japanese_film(env, ("eng", True, {"forced_track": True, "track_name": "Signs"}), ("eng", False, {"codec_id": "S_HDMV/PGS"}), ("fre", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER), "s3": talk.cues(talk.OTHER)})
    hook.main([])
    assert "words" not in env and "subcheck" not in decided(env)


def test_the_check_is_off_with_subtitles_off(env, monkeypatch, settings):
    japanese_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    settings(subtitles="off")
    hook.main([])
    assert "words" not in env and decided(env)["outcome"] == "no_change"


def test_a_hearing_that_fails_gives_unknown_and_the_import_goes_on(env, monkeypatch):
    japanese_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)}, answer={"why": "no answer in 90 seconds"})
    hook.main([])
    rec = decided(env)
    assert rec["subcheck"]["s1"]["verdict"] == "unknown" and rec["subcheck"]["s1"]["why"] == "no answer in 90 seconds"
    assert rec["outcome"] == "no_change" and env["mkvpropedit"] == []


def test_the_check_asks_the_hearing_for_its_time_left(env, monkeypatch):
    """The check shares the job's time limit: with under 10 seconds left it hears nothing and says why."""
    japanese_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    monkeypatch.setattr(hook.DEADLINE, "left", lambda: hook.LID_RESERVE + 5)
    hook.main([])
    r = decided(env)["subcheck"]["s1"]
    assert "words" not in env and r["verdict"] == "unknown" and r["why"].startswith("no time left"), r


def late_english_film(env, monkeypatch, lines=talk.RIGHT, **kw):
    """An English film whose SubRip track shows every line 2 seconds late, or as kw moves it."""
    env["probe"] = sub_probe(("eng", False, {}), audio=(("eng", True),))
    env["movies"]["movie/7"]["runtime"] = round(talk.DURATION / 60)
    hearing(env, monkeypatch, {"s1": talk.cues(lines, **(kw or {"offset": 2.0}))})
    got = []
    def fake_resub(path, j, st, apply, fixes, drop=(), ends=None):
        got.append((apply, fixes, list(drop)))
        if env.get("resub_fails"):
            return "subtitle_remux_failed", "subtitle remux failed: the packet data of stream audio 1 (aac) differ", {"warnings": None}
        return ("subtitles_remuxed", "subtitles remuxed", {"warnings": None, "new_size": 1000, "kept": "/kept/f.mkv"}) if apply else ("would_remux_subtitles", "would remux subtitles: track 2", {})
    monkeypatch.setattr(hook, "resub", fake_resub)
    return got


def test_a_late_subtitle_gets_new_times_in_a_remux(env, monkeypatch):
    got = late_english_film(env, monkeypatch)
    hook.main([])
    rec = [r for r in log_lines(env) if r.get("outcome")][-1]
    (apply, fixes, drop), = got
    assert apply and list(fixes) == [2] and fixes[2]["rate"] == "1/1" and abs(fixes[2]["offset"] - 2) < 0.05 and drop == [], fixes
    assert rec["subremux"]["codes"] == ["subtitle_retimed"] and rec["reasons"][0] == "subtitle_retimed" and rec["subremux"]["rescan"] == "sent"
    assert [r["result"] for r in log_lines(env)][0] == "subtitles remuxed"   # the record is on disk before anything else runs
    assert ("POST", "command", {"name": "RescanMovie", "movieId": 7}) in env["writes"]


def test_a_subtitle_remux_keeps_the_duration_of_the_record(env, monkeypatch):
    """The probe after a subtitle remux gives the record its new size and mtime. Its file_duration stays the one read
    before the remux."""
    late_english_film(env, monkeypatch)
    before, fake = round(hook.arr_decide.duration(env["probe"])), hook.resub
    def resub(*a, **k):
        env["files"][env["path"]]["container"]["properties"]["duration"] += 5 * 10**9   # the new file runs 5 seconds longer
        return fake(*a, **k)
    monkeypatch.setattr(hook, "resub", resub)
    hook.main([])
    rec = decided(env)
    assert rec["subremux"]["done"] and rec["file_duration"] == before, (before, rec.get("file_duration"))


def test_a_retime_in_a_job_process_takes_the_exclusive_lock_first(env, monkeypatch, tmp_path):
    """A job process holds the file lock shared. A remux must not run under it, so the job plans again with the lock
    exclusive, and the second run reads the cached words."""
    got = late_english_film(env, monkeypatch)
    shared = types.SimpleNamespace(exclusive=lambda st: None, reshare=lambda st: None, settle=lambda: None, turn=lambda: None)
    with open(tmp_path / "lock", "w") as lock, pytest.raises(hook.Replan, match="a subtitle needs a remux"):
        hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", 120, job={"app": "radarr"}, lock=lock, shared=shared))
    assert got == []


@pytest.mark.parametrize("kinds, settles", [({"audio", "video"}, 2), ({"audio", "video", "content"}, 1)])
def test_a_job_process_settles_early_unless_wrong_content_may_regrab(env, monkeypatch, settings, tmp_path, kinds, settles):
    """A job process with nothing to re-grab settles before its edit, so the younger jobs of its download go on. Wrong
    content is known only after the edit, so with content in REGRAB it settles only at its end."""
    settings(regrab=kinds)
    calls = []
    shared = types.SimpleNamespace(exclusive=lambda st: None, reshare=lambda st: None, settle=lambda: calls.append(1), turn=lambda: None)
    with open(tmp_path / "lock", "w") as lock:
        hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", 120, lock=lock, shared=shared, job={"app": "radarr"}, post=False))
    assert len(calls) == settles


def test_a_dry_run_only_says_it_would_retime(env, monkeypatch):
    got = late_english_film(env, monkeypatch)
    rec = hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", 120, job={"app": "radarr"}, apply=False, post=False))
    assert got[0][0] is False and rec["subremux"]["codes"] == ["would_remux_subtitles"] and env["mkvpropedit"] == []


@pytest.mark.parametrize("mode", ["import", "backfill", "sub_check", "sub_time", "convert"])
def test_only_an_import_runs_the_audio_and_video_checks(env, monkeypatch, mode):
    """A backfill, --sub-check, --sub-time and --convert leave the audio and video checks to the import and the scans."""
    calls = []
    for name in ("check_audio", "video_check"):
        monkeypatch.setattr(hook, name, lambda *a, real=getattr(hook, name), name=name, **k: calls.append(name) or real(*a, **k))
    job = {"app": "radarr"} if mode == "import" else None
    hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", 120, mode=mode, job=job, apply=False, post=False))
    assert calls == (["check_audio", "video_check"] if mode == "import" else []), calls


def test_an_import_runs_its_steps_in_order(env, monkeypatch):
    """The audio and video checks come before the flag edit. The file lock goes after the edit, and the metadata checks
    and the alerts come after both, so a wrong-content re-grab never runs before the edit or under its lock."""
    events = env["events"]
    for name in ("check_audio", "video_check", "metadata", "alert"):
        monkeypatch.setattr(hook, name, lambda *a, real=getattr(hook, name), name=name, **k: events.append(name) or real(*a, **k))
    real_flock = hook.fcntl.flock
    def flock(f, op):
        if getattr(f, "name", "").endswith("/lock") and op == hook.fcntl.LOCK_UN:
            events.append("unlock")
        real_flock(f, op)
    monkeypatch.setattr(hook.fcntl, "flock", flock)
    hook.main([])
    steps = [e for e in events if e in ("check_audio", "video_check", "mkvpropedit", "unlock", "metadata", "alert")]
    assert steps[:5] == ["check_audio", "video_check", "mkvpropedit", "unlock", "metadata"] and set(steps[5:]) <= {"alert"}, events


def test_the_header_check_reads_the_converted_file(env, monkeypatch):
    """The conversion comes before the header check, so the check reads the new Matroska file."""
    mp4_named_mkv(env)
    seen, real = [], hook.header_of
    monkeypatch.setattr(hook, "header_of", lambda path, j: seen.append(open(path, "rb").read(4)) or real(path, j))
    hook.main([])
    assert seen and set(seen) == {b"MKV!"} and decided(env)["reasons"][0] == "repacked", seen


def test_changed_counts_an_edit_and_a_remux_that_replaced_the_file():
    """An edit counts, also when a wrong-content verdict took the result. --sub-time leaves a conversion out."""
    assert hook.changed({"result": "edited"}) and hook.changed({"result": "would re-grab: x", "edit_result": "edited"})
    assert hook.changed({"result": "no change", "reasons": ["subtitle_retimed"]}, repack=False)
    assert hook.changed({"result": "no change", "reasons": ["repacked"]}) and not hook.changed({"result": "no change", "reasons": ["repacked"]}, repack=False)
    assert not hook.changed({"result": "no change", "reasons": ["would_repair_header"]}) and not hook.changed({"result": "dry run"})


def test_only_an_import_starts_its_time_limit_again():
    for mode in hook.MODES:
        hook.DEADLINE.stop()
        hook.Ctx("radarr", "/m/f.mkv", "f", "English", 0, mode=mode).rearm()
        assert (hook.DEADLINE.left() is not None) == (mode == "import"), mode
    hook.DEADLINE.stop()
    with pytest.raises(ValueError):
        hook.Ctx("radarr", "/m/f.mkv", "f", "English", 0, mode="hook")


def test_subtitles_check_keeps_the_times_and_alerts(env, monkeypatch, settings):
    got = late_english_film(env, monkeypatch)
    settings(subtitles="check")
    hook.main([])
    rec = decided(env)
    assert got == [] and "subremux" not in rec and rec["alert_kinds"] == ["subtiming"], rec
    assert [(x["code"], x["track"]) for x in rec["findings"][0]["lines"]] == [("check_times", "s1")], rec["findings"]


def test_a_cut_with_two_offsets_alerts_and_keeps_the_times(env, monkeypatch):
    got = late_english_film(env, monkeypatch, where=lambda i: 8.0 if talk.FIRST + talk.GAP * i > talk.DURATION / 2 else 0.0)
    hook.main([])
    rec = decided(env)
    assert got == [] and rec["subcheck"]["s1"]["timing"]["piecewise"] and rec["alert_kinds"] == ["subtiming"], rec


def test_a_backfill_checks_subtitles_only_with_sub_check(env, monkeypatch, capsys):
    """Cost: a backfill never hears by default. With --sub-check a dry run reports the verdicts, the fix it would make
    and the summary, and changes nothing. Its verdict asks for an action, so a second dry run checks the file again."""
    got = late_english_film(env, monkeypatch, lines=talk.RIGHT)
    env["probe"] = sub_probe(("eng", False, {}), ("eng", True, {"track_name": "SDH"}), audio=(("eng", True),))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=2.0), "s2": talk.cues(talk.OTHER)})
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr"])
    assert "words" not in env and got == []
    hook.main(["--backfill", "radarr", "--sub-check"])
    out = capsys.readouterr().out
    assert len(env["words"]) == 1 and got == [(False, got[0][1], [])] and env["mkvpropedit"] == [] and env["repacks"] == []
    rec = log_lines(env)[-1]
    s1, s2 = rec["subcheck"]["s1"], rec["subcheck"]["s2"]
    assert (s1["verdict"], s1["timing"]["fix"]["rate"], round(s1["timing"]["fix"]["offset"], 2), s2["verdict"]) == ("match", "1/1", 2.0, "mismatch"), rec["subcheck"]
    assert "would_remux_subtitles" in rec["subremux"]["codes"], rec["subremux"]
    # KEEP_ORIGINALS_DAYS 0 keeps the track in the file. The dry run plans that --apply turns its flags off.
    (line,) = [x for f in rec["findings"] for x in f["lines"] if x["code"] == "stays"]
    assert not rec["apply"] and (line["track"], line["kept_back"], line["flags_off"], line["gone"]) == ("s2", "keep_days", True, False), line
    assert "subtitle check: 1 files checked, 1 tracks match, 1 do not, 0 unknown, 1 timing fixes, 12 CPU seconds" in out, out
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert len(env["words"]) == 2


def test_a_backfill_with_sub_check_skips_a_file_it_checked(env, monkeypatch, capsys):
    """A cached verdict that asks for nothing more skips the file, so a stopped run goes on where it stopped. A file that
    changed since is checked again."""
    japanese_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert len(env["words"]) == 1 and hook.sub_cached(env["path"])
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert len(env["words"]) == 1 and '"subtitles_cached": 1' in capsys.readouterr().out
    os.utime(env["path"], ns=(time.time_ns(), os.stat(env["path"]).st_mtime_ns + 10**9))
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert len(env["words"]) == 2


def test_sub_check_runs_with_the_flag_backfill_only(env, monkeypatch):
    for extra in (["--convert"], ["--check-audio"]):
        with pytest.raises(SystemExit) as ex:
            hook.main(["--backfill", "radarr", "--sub-check", *extra])
        assert ex.value.code == 2


def test_a_backfill_takes_only_the_listed_paths(env, monkeypatch, tmp_path, capsys):
    other = tmp_path / "media" / "Other.mkv"
    other.write_bytes(b"x")
    movies = [dict(env["movies"]["movie/7"], id=i, title=f"Film {i}", movieFile={"id": i, "path": p}) for i, p in ((7, env["path"]), (8, str(other)))]
    monkeypatch.setattr(hook, "arr", lambda app, p: movies)
    seen = []
    monkeypatch.setattr(hook, "process", lambda ctx: seen.append(ctx.path) or {"outcome": "no_change", "result": "no change"})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--paths", str(other), "/nowhere.mkv"])
    assert seen == [str(other)] and "not in this run's work list, skipped: /nowhere.mkv" in capsys.readouterr().out


def removal_film(env, monkeypatch, tmp_path, fails=False):
    """An English film whose default English subtitle holds another episode, beside a matching SDH track. Originals are
    kept, and the fake remux removes the tracks it drops from the file's probe."""
    english_film(env, ("eng", True, {}), ("eng", False, {"track_name": "SDH"}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER), "s2": talk.cues(talk.RIGHT)})
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    got, env["resub_fails"] = [], fails

    def fake_resub(path, j, st, apply, fixes, drop=(), ends=None):
        got.append((apply, fixes, list(drop)))
        if fails:
            return "subtitle_remux_failed", "subtitle remux failed: the packet data of stream audio 1 (aac) differ", {"warnings": None}
        if not apply:
            return "would_remux_subtitles", "would remux subtitles: remove track 2", {}
        p = env["files"][path]
        p["tracks"] = [t for t in p["tracks"] if t.get("id") not in drop]
        return "subtitles_remuxed", "subtitles remuxed", {"warnings": None, "new_size": 1000, "kept": "/kept/f.mkv"}
    monkeypatch.setattr(hook, "resub", fake_resub)
    return got


def test_a_track_of_another_episode_is_removed_in_a_remux(env, monkeypatch, tmp_path):
    """Rather no subtitle than a wrong one: the track leaves the file and the original is kept. The SDH track, now s1,
    stays off under the English audio, so no flag changes."""
    got = removal_film(env, monkeypatch, tmp_path)
    hook.main([])
    rec = decided(env)
    assert got == [(True, {}, [2])] and rec["subremux"]["removed"] == ["s1"] and rec["subremux"]["codes"] == ["subtitle_mismatch_removed"], rec
    assert not rec.get("edits") and rec["outcome"] == "no_change" and "subtitle_audio_mismatch" not in rec["reasons"], rec
    assert rec["alert_kinds"] == ["submatch"], rec
    assert [(x["code"], x["track"], x["by"], x["kept"]) for x in rec["findings"][0]["lines"]] == [("removed", "s1", "hook", "/kept/f.mkv")], rec["findings"]
    assert rec["subremux"]["rescan"] == "sent"


def test_an_overrun_after_a_removal_names_the_track_by_its_place_before_the_remux(env, monkeypatch, tmp_path):
    """The remux removes s1, and the SDH track that was s2 runs past the end in a format no trim can cut. The header
    probe after the remux calls it s1. The alert names it by its place before the remux, as the removed track's
    sentence does, so it never names the removed track."""
    removal_film(env, monkeypatch, tmp_path)
    header_issue(env, monkeypatch, repairable=False)
    def header_of(path, j=None):   # the overrun track is the last subtitle track, before and after the remux
        n = sum(t["type"] == "subtitles" for t in j["tracks"])
        return dict(HEADER_HP, issue=["a subtitle event runs to 26:01, past the video and the audio at 23:52"], blocked=["x"],
                    unfixable=[{"track": f"s{n}", "codec": "S_TEXT/ASS", "end": 1561.0, "streams": 1432.0}])
    monkeypatch.setattr(hook, "header_of", header_of)
    hook.main([])
    rec = decided(env)
    assert rec["subremux"]["removed"] == ["s1"] and rec["header_repair"]["code"] == "subtitle_overrun_unfixable", rec
    (f,) = [f for f in rec["findings"] if f["kind"] == "subtitle"]
    assert [x["track"] for x in f["tracks"]] == ["s2"], f
    assert [a for a in rec["alerts"] if a.startswith("subtitle: ")] == [
        "subtitle: The English subtitles (track 2) keep going until 26:01, but the video and audio end at 23:52. They're in ASS format, which "
        "can't be trimmed automatically, so they were left as they are."]
    assert [hook.place_before(p, gone) for p, gone in (("s1", []), ("s1", ["s1"]), ("s2", ["s1", "s3"]))] == ["s1", "s2", "s4"]


def test_a_failed_removal_turns_the_flags_off_instead(env, monkeypatch, tmp_path):
    got = removal_film(env, monkeypatch, tmp_path, fails=True)
    hook.main([])
    rec = decided(env)
    assert got == [(True, {}, [2])] and rec["subremux"]["codes"] == ["subtitle_remux_failed"] and rec["subremux"]["removed"] == []
    assert rec["edits"] == [["track:=10", 0, 1]] and "subtitle_audio_mismatch" in rec["reasons"], rec
    (line,) = rec["findings"][0]["lines"]
    assert (line["code"], line["gone"]) == ("stays", True) and line["result"].startswith("subtitle remux failed: the packet data"), line


def test_no_kept_original_means_no_removal(env, monkeypatch, settings, tmp_path):
    """A removal cannot be undone without the kept original, so KEEP_ORIGINALS_DAYS 0 keeps the track and turns it off."""
    got = removal_film(env, monkeypatch, tmp_path)
    settings(keep_days=0)
    hook.main([])
    rec = decided(env)
    assert got == [] and rec["edits"] == [["track:=10", 0, 1]] and "subtitle_audio_mismatch" in rec["reasons"], rec
    (line,) = rec["findings"][0]["lines"]
    assert (line["code"], line["gone"], line["kept_back"]) == ("stays", False, "keep_days"), line


def test_a_retime_and_a_removal_share_one_remux(env, monkeypatch, settings, tmp_path):
    got = late_english_film(env, monkeypatch)
    env["probe"] = sub_probe(("eng", False, {}), ("eng", False, {"track_name": "SDH"}), audio=(("eng", True),))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=2.0), "s2": talk.cues(talk.OTHER)})
    settings(keep_days=7)
    hook.main([])
    rec = decided(env)
    (apply, fixes, drop), = got
    assert apply and list(fixes) == [2] and drop == [3] and rec["subremux"]["codes"] == ["subtitle_retimed", "subtitle_mismatch_removed"], got


def test_a_dry_run_backfill_with_sub_check_plans_the_removal(env, monkeypatch, tmp_path, capsys):
    got = removal_film(env, monkeypatch, tmp_path)
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert got == [(False, {}, [2])] and env["mkvpropedit"] == []
    hook.main(["--backfill", "radarr", "--sub-check", "--apply"])
    assert got[1:] == [(True, {}, [2])], got


def sidecar_film(env, monkeypatch, tmp_path, **names):
    """An English film with no subtitle track and sidecars {name suffix: cues} beside it. Originals are kept under
    tmp_path/.kept."""
    env["probe"] = sub_probe(audio=(("eng", True),))
    env["movies"]["movie/7"]["runtime"] = round(talk.DURATION / 60)
    for ext, cs in names.items():
        with open(env["path"][:-4] + ext.replace("_", "."), "w") as f:
            f.write(srt_text(cs))
    hearing(env, monkeypatch, {})
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))


def srt_text(cs):
    t = lambda x: f"{int(x // 3600):02d}:{int(x // 60 % 60):02d}:{int(x % 60):02d},{round(x % 1 * 1000):03d}"
    return "".join(f"{i}\n{t(a)} --> {t(b)}\n{text}\n\n" for i, (a, b, text) in enumerate(cs, 1))


def test_a_sidecar_beside_an_mkv_that_does_not_match_moves_to_the_kept_originals(env, monkeypatch, tmp_path):
    sidecar_film(env, monkeypatch, tmp_path, _en_srt=talk.cues(talk.OTHER), _en_forced_srt=talk.cues(talk.OTHER))
    hook.main([])
    rec = decided(env)
    side = env["path"][:-4] + ".en.srt"
    assert set(rec["subcheck"]) == {os.path.basename(side)}   # a forced sidecar is never checked
    assert not os.path.exists(side) and os.path.exists(env["path"][:-4] + ".en.forced.srt")
    (e,), = [rec["sidecars"]]
    assert e["result"] == "moved" and e["kept"].startswith(str(tmp_path / ".kept")) and open(e["kept"]).read() == srt_text(talk.cues(talk.OTHER))
    assert [r["result"] for r in log_lines(env) if r.get("path") == side] == ["sidecar moved"]
    assert rec["alert_kinds"] == ["submatch"], rec
    assert [(x["code"], x["name"], x["kept"]) for x in rec["findings"][0]["lines"]] == [("sidecar", os.path.basename(side), e["kept"])], rec["findings"]


def test_subtitles_check_reports_a_wrong_track_and_sidecar_and_changes_nothing(env, monkeypatch, settings, tmp_path):
    """SUBTITLES check reads, reports and alerts. The wrong track keeps its flags, and the wrong sidecar stays."""
    sidecar_film(env, monkeypatch, tmp_path, _en_srt=talk.cues(talk.OTHER))
    settings(subtitles="check")
    side = env["path"][:-4] + ".en.srt"
    hook.main([])
    rec = decided(env)
    assert rec["subcheck"][os.path.basename(side)]["verdict"] == "mismatch" and os.path.exists(side) and env["mkvpropedit"] == []
    (e,) = rec["sidecars"]
    assert (e["result"], e["left"]) == ("left", "SUBTITLES is set to check")
    assert rec["alert_kinds"] == ["submatch"], rec
    assert [(x["code"], x["kept"], x["left"]) for x in rec["findings"][0]["lines"]] == [("sidecar", None, "SUBTITLES is set to check")]


@pytest.mark.parametrize("level", ["check", "fix"])
def test_subtitles_check_leaves_the_verdict_out_of_the_flags(env, monkeypatch, settings, level):
    """The policy turns the English subtitle off under English audio either way. Only fix lets the verdict decide too."""
    settings(keep_days=7)
    english_film(env, ("eng", True, {}), ("eng", False, {"track_name": "SDH"}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER), "s2": talk.cues(talk.RIGHT)})
    removed = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: removed.append(list(drop)) or ("would_remux_subtitles", "would remux", {}))
    settings(subtitles=level)
    hook.main([])
    rec = decided(env)
    assert rec["subcheck"]["s1"]["verdict"] == "mismatch" and ("subtitle_audio_mismatch" in rec["reasons"]) == (level == "fix"), rec["reasons"]
    assert removed == ([] if level == "check" else [[2]])   # fix removes s1, track id 2. check never remuxes.
    if level == "check":   # the track stays in the file, and its flags stay
        line = rec["findings"][0]["lines"][-1]
        assert (line["code"], line["gone"], line["kept_back"], line["flags_off"]) == ("stays", False, "check", False), line


@pytest.mark.parametrize("level", ["off", "check"])
def test_sub_check_and_sub_time_fix_whatever_subtitles_says(env, monkeypatch, settings, level):
    got = late_english_film(env, monkeypatch)
    settings(subtitles=level)
    hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", 120, mode="sub_check", post=False))
    (apply, fixes, drop), = got
    assert apply and list(fixes) == [2]
    assert hook.sub_on("backfill", True) and not hook.sub_on("backfill", False) and hook.sub_fixes("backfill")
    assert hook.sub_on("hook") == (level != "off") and not hook.sub_fixes("hook") and not hook.sub_fixes("deep_analysis")
    assert hook.sub_on("deep_analysis", True) == (level != "off")   # a deep analysis follows SUBTITLES, though it runs as --sub-time


@pytest.mark.parametrize("level, sub", [("check", False), ("fix", True), ("deep", True), ("off", False)])
def test_the_conversion_checks_subtitles_only_where_it_may_fix(env, monkeypatch, settings, level, sub):
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    settings(subtitles=level)
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    seen = []
    monkeypatch.setattr(hook, "convert", lambda app, path, j, st, apply, ids=None, lock=None, pool=None, force=None, sub=False, known=(), shared=False,
                        source=None: seen.append(sub) or ("not_matroska", "skipped, not matroska, test", {}, path))
    hook.main([])
    assert seen == [sub]


def test_a_late_sidecar_beside_an_mkv_is_written_again_and_its_original_kept(env, monkeypatch, tmp_path):
    sidecar_film(env, monkeypatch, tmp_path, _en_srt=talk.cues(talk.RIGHT, offset=2.0))
    side = env["path"][:-4] + ".en.srt"
    os.chmod(side, 0o640)
    hook.main([])
    rec = decided(env)
    (e,), = [rec["sidecars"]]
    assert e["result"] == "retimed" and rec["alert_kinds"] == [], rec
    assert open(e["kept"]).read() == srt_text(talk.cues(talk.RIGHT, offset=2.0))   # the kept link holds the old text
    got = hook.srt_cues(open(side).read())
    assert all(abs(a / 1000 - c[0]) <= 0.3 for (a, _, _), c in zip(got, talk.cues(talk.RIGHT))) and os.stat(side).st_mode & 0o777 == 0o640
    assert not os.path.exists(os.path.join(os.path.dirname(side), hook.CFG.hide_dir))   # the temp file and its folder are gone


def test_a_sidecar_with_no_place_to_keep_its_original_stays(env, monkeypatch, settings, tmp_path):
    sidecar_film(env, monkeypatch, tmp_path, _en_srt=talk.cues(talk.OTHER))
    settings(keep_days=0)
    hook.main([])
    rec = decided(env)
    assert rec["sidecars"][0]["result"] == "left" and os.path.exists(env["path"][:-4] + ".en.srt")
    line = rec["findings"][0]["lines"][0]
    assert (line["code"], line["kept"]) == ("sidecar", None) and line["left"].startswith("KEEP_ORIGINALS_DAYS is 0"), line


def test_a_new_sidecar_undoes_the_backfill_skip(env, monkeypatch, tmp_path, capsys):
    """A file with only a sidecar is taken by --sub-check. A sidecar Bazarr writes later makes the file new again."""
    sidecar_film(env, monkeypatch, tmp_path, _en_srt=talk.cues(talk.RIGHT))
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr"])
    assert "words" not in env   # one audio track and no subtitle track: the flag backfill leaves it out
    hook.main(["--backfill", "radarr", "--sub-check"])
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert len(env["words"]) == 1
    with open(env["path"][:-4] + ".en.sdh.srt", "w") as f:
        f.write(srt_text(talk.cues(talk.RIGHT)))
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert len(env["words"]) == 2


def test_a_window_that_hears_too_little_gets_a_third_in_its_part(env, monkeypatch):
    """Cost: a window that heard too little gets a longer window in the same part of the file, heard by arr_lid in the
    same process, so the model loads once. A file whose windows hear enough pays nothing for it."""
    track = talk.cues(talk.OTHER)
    first = hook.arr_subsync.windows(track, talk.DURATION, hook.arr_decide.STOPWORDS["eng"])
    english_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": track}, spoken=lambda i: not first[0] - 1 <= talk.FIRST + talk.GAP * i < first[0] + hook.arr_subsync.WINDOW)
    hook.main([])
    r = decided(env)["subcheck"]["s1"]
    (_, _, a), = env["words"]
    (b,), = env["more"]
    assert a == first and b + hook.arr_subsync.THIRD <= 0.25 * talk.DURATION, (a, b)
    assert b + hook.arr_subsync.THIRD <= first[0] or b >= first[0] + hook.arr_subsync.WINDOW   # no overlap with the first
    assert r["verdict"] == "mismatch" and len(r["windows"]) == 3 and r["starts"] == [[a, hook.arr_subsync.WINDOW]], r


def test_no_third_window_when_both_windows_hear_too_little(env, monkeypatch):
    """Two short windows and a third could never make two good ones, so the first hearing hears no third. The drift
    hearing then tries where a far ratio would put the speech, and with too little speech there too, it stays unknown."""
    japanese_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)}, spoken=lambda i: i % 5 == 0)
    hook.main([])
    assert env["more"][0] == [] and len(env["words"]) == 2 and decided(env)["subcheck"]["s1"]["verdict"] == "unknown"


def test_the_windows_of_an_earlier_check_are_heard_again_as_they_were(env, monkeypatch):
    """After a conversion the new file's check hears the same windows, so each hearing reads the words carried over."""
    hearing(env, monkeypatch, {})
    j = sub_probe(audio=(("eng", True),))
    items = {"x": ("eng", 0, talk.cues(talk.OTHER))}
    hook.sub_verdicts(env["path"], j, items, {0: [[[100.0, 1000.0], 10.0], [[150.0], 24.0]]})
    assert [w for _, _, w in env["words"]] == [[100.0, 1000.0], [150.0]]


def test_renumber_moves_the_later_tracks_up():
    assert hook.renumber({"s1": "rum", "s3": "fre", "a1": "eng", "s4": "x"}, {"s2", "s4"}) == {"s1": "rum", "s2": "fre", "a1": "eng"}


def test_the_verdict_cache_keeps_a_pending_action(tmp_path, monkeypatch, settings):
    """A verdict whose action no apply made yet does not skip the file, so an apply after a dry run still acts."""
    settings(state_dir=str(tmp_path))
    path = str(tmp_path / "f.mkv")
    open(path, "w").close()
    assert not hook.sub_cached(path)
    hook.sub_cache(path, {"s1": "mismatch"}, True)
    assert not hook.sub_cached(path)
    hook.sub_cache(path, {"s1": "mismatch"}, False)
    assert hook.sub_cached(path)


def test_decide_never_gives_the_default_to_an_unmatched_track():
    """The unmatched role is in no policy list. An unmatched forced-flagged track loses the forced flag too."""
    d = hook.arr_decide
    j = sub_probe(("eng", True, {"forced_track": True}), ("eng", False, {"track_name": "SDH"}))
    for t in j["tracks"]:
        if t["type"] == "subtitles":
            t["properties"]["tag_number_of_frames"] = "900"   # dense, so the forced flag reads as a full track
    p = d.decide(j, "Japanese", unmatched={"s1"})
    assert ["track:=10", 0, 1, d.FORCED_FLAG] in p["edits"] and ["track:=10", 0, 1] in p["edits"] and ["track:=11", 1, 0] in p["edits"], p
    assert p["tracks"][2]["role"] == "unmatched" and not p["dropped"]


@pytest.mark.parametrize("seconds, heard", [(hook.SUB_MIN_SECONDS - 1, False), (hook.SUB_MIN_SECONDS, True)])
def test_a_short_file_is_never_heard(env, monkeypatch, seconds, heard):
    """Two windows of a file under SUB_MIN_SECONDS sit too close for a timing fit. A file of SUB_MIN_SECONDS is heard."""
    english_film(env, ("eng", False, {}))
    env["probe"]["container"]["properties"]["duration"] = seconds * 10**9
    env["movies"]["movie/7"]["runtime"] = 5
    hearing(env, monkeypatch, {"s1": [(10.0 + 2 * i, 11.0 + 2 * i, talk.OTHER[i]) for i in range(140)]})
    hook.main([])
    assert ("words" in env) == heard, decided(env).get("subcheck")


def test_props_fault_names_what_a_remux_lost():
    j = sub_probe(("eng", False, {"language_ietf": "en-US"}))
    j["attachments"] = [{"id": 1}]
    assert hook.props_fault(j, copy.deepcopy(j)) is None
    for change, why in ((lambda n: n["tracks"][3]["properties"].update(uid=99), "track 3 changed its uid"),
                        (lambda n: n["tracks"][3]["properties"].pop("language_ietf"), "track 3 changed its language_ietf"),
                        (lambda n: n["tracks"][3]["properties"].update(flag_hearing_impaired=True), "track 3 changed its flag_hearing_impaired"),
                        (lambda n: n.update(attachments=[]), "the attachments changed from 1 to 0")):
        new = copy.deepcopy(j)
        change(new)
        assert hook.props_fault(j, new) == why


def test_srt_moved_moves_only_the_times():
    text = "1\n00:00:01,000 --> 00:00:03,500\n<i>Mira</i>, open it.\n\n2\n00:01:00,250 --> 00:01:02,000\nNot yet.\n"
    got = hook.srt_moved(text, {"rate": "1/1", "offset": 2.0})
    assert got == "1\n00:00:00,000 --> 00:00:01,500\n<i>Mira</i>, open it.\n\n2\n00:00:58,250 --> 00:01:00,000\nNot yet.\n"


def test_convert_subs_leaves_out_what_does_not_match(monkeypatch, settings, tmp_path):
    """A conversion: the sidecar that does not match moves into the kept originals and is not muxed, the late sidecar is
    muxed from a copy with new times while its original is kept, and the built-in track that does not match is left
    out of the remux."""
    video = tmp_path / "media" / "Film A (1979).mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    stamp = lambda s: f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{int(s % 60):02d},000"   # SubRip times, whole seconds
    srt = lambda cs: "".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{t}\n\n"
                             for i, (a, b, t) in enumerate(cs, 1))
    for name, cs in (("Film A (1979).en.srt", talk.cues(talk.OTHER)), ("Film A (1979).en.sdh.srt", talk.cues(talk.RIGHT, offset=2.0)),
                     ("Film A (1979).en.forced.srt", talk.cues(talk.OTHER))):   # a forced sidecar is never checked
        (video.parent / name).write_text(srt([(round(a), round(b), t) for a, b, t in cs]))
    subs = hook.sidecar_subs(str(video))
    j = sub_probe(("eng", False, {}), audio=(("eng", True),))
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}, {"index": 1, "codec_type": "audio", "codec_name": "aac"},
               {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text"}]
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=srt([(1, 2, "Hello")] * 30), stderr=""))
    verdicts = {"Film A (1979).en.srt": {"verdict": "mismatch", "why": "the heard words match the cues at 5%, 8% at best", "timing": None},
                "Film A (1979).en.sdh.srt": {"verdict": "match", "why": "", "timing": {"fix": {"rate": "1/1", "offset": 2.0}}},
                "s1": {"verdict": "mismatch", "why": "the heard words match the cues at 0%, 3% at best", "timing": None}}
    asked = []
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, starts=None, line=True, deep=False, streams=None: asked.append(sorted(items)) or verdicts)
    info = {}
    keep, drop = hook.convert_subs(str(video), j, streams, subs, talk.DURATION, True, str(tmp_path), info)
    assert asked == [sorted(verdicts)] and drop == [(2, 2)] and info["tracks_unmatched"] == ["s1"]
    assert [s["name"] for s in keep] == ["Film A (1979).en.forced.srt", "Film A (1979).en.sdh.srt"] and keep[0].get("mux") is None
    keep = keep[1:]
    assert keep[0]["mux"].startswith(str(tmp_path)) and keep[0]["charset"] == "UTF-8"
    assert not (video.parent / "Film A (1979).en.srt").exists() and info["sidecars_unmatched"][0]["moved"].startswith(str(tmp_path / ".kept"))
    assert info["sidecars_kept"][0].endswith("Film A (1979).en.sdh.srt") and (video.parent / "Film A (1979).en.sdh.srt").exists()
    assert open(keep[0]["mux"]).read().startswith("1\n00:01:00,000 --> 00:01:02,000\n")   # the first cue, at 62 s, moved 2 s earlier
    argv = hook.convert_cmd(str(video), "/t.mkv", {"tracks": [{"id": 0}, {"id": 1}, {"id": 2}]}, keep, drop=[2])
    assert argv[argv.index("--track-order") + 1] == "0:0,0:1,1:0" and argv[argv.index("-s") + 1] == "!2" and argv[-1] == keep[0]["mux"]
    (video.parent / "Film A (1979).en.forced.srt").unlink()
    # with no place to keep it, the unmatched sidecar stays beside the file and says why
    (video.parent / "Film A (1979).en.srt").write_text(srt([(round(a), round(b), t) for a, b, t in talk.cues(talk.OTHER)]))
    settings(keep_days=0)
    info = {}
    hook.convert_subs(str(video), j, streams, hook.sidecar_subs(str(video)), talk.DURATION, True, str(tmp_path), info)
    assert (video.parent / "Film A (1979).en.srt").exists() and info["sidecars_unmatched"][0]["left"] == "KEEP_ORIGINALS_DAYS is 0"


@pytest.fixture(scope="module")
def sync_mkv(tmp_path_factory):
    """A Matroska file of 40 seconds with video, audio and one SubRip track of 18 cues, one every 2 seconds."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge")):
        pytest.skip("needs ffmpeg and mkvmerge")
    d = tmp_path_factory.mktemp("sync")
    with open(d / "s.srt", "w") as f:
        f.write("".join(f"{i}\n00:00:{2 * i:02d},000 --> 00:00:{2 * i + 1:02d},500\n{talk.RIGHT[i]}\n\n" for i in range(1, 19)))
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=40:size=160x90:rate=25", "-f", "lavfi", "-i",
              "sine=frequency=440:duration=40", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac", str(d / "av.mp4")], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "f.mkv"), str(d / "av.mp4"), "--language", "0:eng", str(d / "s.srt")], check=True)
    return d


def test_subtitle_cues_read_the_times_of_each_cue(sync_mkv):
    path = str(sync_mkv / "f.mkv")
    got = hook.subtitle_cues(path, REAL_MKVMERGE(path), {"s1"})["s1"]
    assert [(a, b) for a, b, _ in got] == [(2.0 * i, 2.0 * i + 1.5) for i in range(1, 19)] and got[0][2] == talk.RIGHT[1]


@pytest.mark.parametrize("fix", [{"rate": "1/1", "offset": 1.0}, {"rate": "25025/24000", "offset": 0.0}])
def test_resub_moves_the_track_and_proves_every_other_packet(sync_mkv, tmp_path, fix):
    """A real remux: ffmpeg moves the cues by the fix, and the proof shows every video and audio packet the same. The
    tracks keep their UIDs, so the flag edits of the plan still find them."""
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    j = REAL_MKVMERGE(path)
    _, result, info = hook.resub(path, j, os.stat(path), True, {2: fix})
    assert result == "subtitles remuxed", (result, info)
    assert all(e["match"] for e in info["proof"]) and [e["stream"] for e in info["proof"]] == ["video 0", "audio 1", "subtitle 2"]
    new = REAL_MKVMERGE(path)
    assert [t["properties"]["uid"] for t in new["tracks"]] == [t["properties"]["uid"] for t in j["tracks"]]
    got = hook.subtitle_cues(path, new, {"s1"})["s1"]
    want = [hook.arr_subsync.moved(2000 * i, fix) / 1000 for i in range(1, 19)]
    assert len(got) == len(want) and all(abs(a - max(0, w)) <= 0.002 for (a, _, _), w in zip(got, want)), (got[:3], want[:3])


def test_the_proof_refuses_times_the_fix_does_not_explain(sync_mkv, tmp_path):
    """The guard of the retime: the proof reads the moved track as moved only with the fix, and refuses a wrong fix."""
    src, tmp = str(sync_mkv / "f.mkv"), str(tmp_path / "t.mkv")
    REAL_RUN(["ffmpeg", "-v", "error", "-i", src, "-itsoffset", "-1", "-i", src, "-map", "0:0", "-map", "0:1", "-map", "1:2", "-c", "copy", "-copyinkf",
              tmp], check=True)
    fix = {"rate": "1/1", "offset": 1.0}
    assert proved(src, tmp, [], str(tmp_path), retimed={0: fix})[0] is None
    assert "subtitle 2" in proved(src, tmp, [], str(tmp_path))[0]
    assert "subtitle 2" in proved(src, tmp, [], str(tmp_path), retimed={0: {"rate": "1/1", "offset": 0.5}})[0]


def test_the_proof_leaves_out_a_dropped_track_only_when_asked(sync_mkv, tmp_path):
    src, tmp = str(sync_mkv / "f.mkv"), str(tmp_path / "t.mkv")
    REAL_RUN(["ffmpeg", "-v", "error", "-i", src, "-map", "0:0", "-map", "0:1", "-c", "copy", "-copyinkf", tmp], check=True)
    assert proved(src, tmp, [], str(tmp_path), dropped={2})[0] is None
    assert proved(src, tmp, [], str(tmp_path))[0] == "the new file holds 0 subtitle streams, not 1"


def test_resub_removes_one_track_and_retimes_another_in_one_remux(sync_mkv, tmp_path):
    """A real remux of a file with two SubRip tracks: the first moves 1 s earlier, the second leaves the file. The
    tracks that stay keep their UIDs and flags, the proof passes, and a removal keeps the original."""
    src, path = str(sync_mkv / "f.mkv"), str(tmp_path / "two.mkv")
    REAL_RUN(["mkvmerge", "-q", "-o", path, src, "--language", "0:eng", "--track-name", "0:Other", str(sync_mkv / "s.srt")], check=True)
    j = REAL_MKVMERGE(path)
    fix = {"rate": "1/1", "offset": 1.0}
    old = hook.CFG, hook.originals_root
    hook.CFG, hook.originals_root = dataclasses.replace(hook.CFG, keep_days=7), lambda p: str(tmp_path / ".kept")
    try:
        _, result, info = hook.resub(path, j, os.stat(path), True, {2: fix}, [3])
    finally:
        hook.CFG, hook.originals_root = old
    assert result == "subtitles remuxed", (result, info)
    assert [e["stream"] for e in info["proof"]] == ["video 0", "audio 1", "subtitle 2"] and all(e["match"] for e in info["proof"])
    new = REAL_MKVMERGE(path)
    assert [t["properties"]["uid"] for t in new["tracks"]] == [t["properties"]["uid"] for t in j["tracks"][:3]]
    assert info["kept"].startswith(str(tmp_path / ".kept")) and REAL_MKVMERGE(info["kept"])["tracks"][3]["properties"]["track_name"] == "Other"
    got = hook.subtitle_cues(path, new, {"s1"})["s1"]
    assert [round(a, 2) for a, _, _ in got[:2]] == [1.0, 3.0]


def test_a_translated_subtitle_beside_a_dub_is_never_removed(env, monkeypatch, settings, tmp_path):
    """A Japanese series with Japanese audio and an English dub. The English subtitle translates the Japanese, so its
    words differ from the dub's. A mismatch there is only unknown: no removal, no flag change, no alert. The same
    holds for a sidecar beside it. A match still counts."""
    japanese_film(env, ("eng", True, {}), ("eng", False, {"track_name": "SDH"}))
    with open(env["path"][:-4] + ".en.srt", "w") as f:
        f.write(srt_text(talk.cues(talk.OTHER)))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER), "s2": talk.cues(talk.RIGHT)})
    settings(keep_days=7)
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("a translated subtitle must never be removed"))
    hook.main([])
    rec = decided(env)
    side = os.path.basename(env["path"][:-4] + ".en.srt")
    assert [rec["subcheck"][k]["verdict"] for k in ("s1", "s2", side)] == ["unknown", "match", "unknown"], rec["subcheck"]
    assert rec["subcheck"]["s1"]["why"].endswith(", but the original language is jpn, and the subtitle may translate it")
    assert rec["subcheck"]["s1"]["held"] and "sidecars" not in rec and os.path.exists(env["path"][:-4] + ".en.srt")
    assert "submatch" not in rec["alert_kinds"] and "subtitle_audio_mismatch" not in rec["reasons"]


def test_a_dub_only_file_of_a_foreign_original_never_loses_a_subtitle(env, monkeypatch):
    """A Japanese film that carries only its English dub. The English subtitle may translate the Japanese original,
    so a mismatch is only unknown. Near miss: the same file as an English original keeps its mismatch."""
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Japanese"}
    hook.main([])
    r = decided(env)["subcheck"]["s1"]
    assert r["verdict"] == "unknown" and r["why"].endswith(", but the original language is jpn, and the subtitle may translate it"), r
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "English"}
    os.utime(env["path"], ns=(time.time_ns(), os.stat(env["path"]).st_mtime_ns + 10**9))
    hook.main([])
    assert [r for r in log_lines(env) if r.get("outcome")][-1]["subcheck"]["s1"]["verdict"] == "mismatch"


def test_sub_hold_takes_the_original_first_and_the_other_audio_only_without_one():
    ts = lambda *audio: hook.arr_decide.classify(sub_probe(audio=[(a, i == 0) for i, a in enumerate(audio)]))
    assert hook.sub_hold(ts("eng", "spa"), {"eng"}, "eng") is None   # an English original with a Spanish dub
    assert hook.sub_hold(ts("jpn", "eng"), {"jpn", "jap"}, "eng").startswith("the original language is jpn")
    assert hook.sub_hold(ts("eng"), {"jpn", "jap"}, "eng").startswith("the original language is jpn")   # a dub-only file
    assert hook.sub_hold(ts("jpn", "eng"), set(), "eng").startswith("no original language is known, and the file also carries jpn audio")
    assert hook.sub_hold(ts("eng"), set(), "eng") is None and hook.sub_hold(ts("eng", "und"), set(), "eng") is None


def guarded_film(env, monkeypatch, original, audio):
    """A film of original language original with the audio tracks audio and one English subtitle of another episode."""
    env["movies"]["movie/7"].update(originalLanguage={"name": original}, runtime=round(talk.DURATION / 60))
    env["probe"] = sub_probe(("eng", False, {}), audio=[(a, i == 0) for i, a in enumerate(audio)])
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    hook.main([])
    return decided(env)["subcheck"]["s1"]


def test_an_english_original_with_a_spanish_dub_keeps_its_mismatch(env, monkeypatch):
    r = guarded_film(env, monkeypatch, "English", ("eng", "spa"))
    assert r["verdict"] == "mismatch" and "held" not in r, r


def test_an_unknown_original_with_two_audio_languages_holds_the_mismatch(env, monkeypatch):
    r = guarded_film(env, monkeypatch, "Unknown", ("jpn", "eng"))
    assert r["verdict"] == "unknown" and r["held"] and "no original language is known" in r["why"], r


def test_an_unknown_original_with_one_audio_language_keeps_its_mismatch(env, monkeypatch):
    r = guarded_film(env, monkeypatch, "Unknown", ("eng",))
    assert r["verdict"] == "mismatch", r


def test_a_conversion_keeps_a_translated_sidecar(monkeypatch, settings, tmp_path):
    """The guard holds in a conversion too: a mismatch of a dub-only foreign original is unknown, so the sidecar is
    muxed and stays."""
    video = tmp_path / "Film A (1979).mp4"
    video.write_bytes(b"x")
    (tmp_path / "Film A (1979).en.srt").write_text(srt_text(talk.cues(talk.OTHER)))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, starts=None, line=True, deep=False, streams=None: {k: {"verdict": "mismatch", "why": "5%", "timing": None} for k in items})
    subs, info = hook.sidecar_subs(str(video)), {}
    keep, drop = hook.convert_subs(str(video), sub_probe(audio=(("eng", True),)), [], subs, talk.DURATION, True, str(tmp_path), info, {"jpn", "jap"})
    assert [s["name"] for s in keep] == ["Film A (1979).en.srt"] and drop == [] and "sidecars_unmatched" not in info
    assert info["subcheck"]["Film A (1979).en.srt"]["verdict"] == "unknown"


def test_a_conversion_that_leaves_a_track_out_keeps_the_original(env, monkeypatch, settings):
    """A track that leaves the file comes back only from the kept original, so the conversion keeps it, as a forced
    one does. The alert names where it is."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    folder = os.path.dirname(mkv)
    root = os.path.join(os.path.dirname(os.path.dirname(folder)), hook.CFG.keep_dir)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: root)

    def left_out(path, j, streams, subs, dur, apply, folder, info, known=()):
        info.update(subcheck={"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 5%, 8% at best"}}, tracks_unmatched=["s1"])
        return subs, [(2, 2)]
    monkeypatch.setattr(hook, "convert_subs", left_out)
    monkeypatch.setattr(hook, "sub_on", lambda source, sub_check=False: True)
    hook.main([])
    rec = decided(env)
    remux = env["repacks"][0]
    assert remux[remux.index("-s") + 1] == "!2" and rec["repack"]["kept"].startswith(root + "/") and os.path.exists(rec["repack"]["kept"])
    assert ("converted_track", "s1", rec["repack"]["kept"]) in [(x["code"], x.get("track"), x["kept"]) for f in rec["findings"] for x in f["lines"]], rec["findings"]


def test_a_conversion_with_no_place_to_keep_the_original_keeps_the_track(monkeypatch, settings, tmp_path):
    """With KEEP_ORIGINALS_DAYS 0 the track stays in, and the new file's check turns its flags off."""
    video = tmp_path / "Film A (1979).mp4"
    video.write_bytes(b"x")
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}, {"index": 1, "codec_type": "audio", "codec_name": "aac"},
               {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text"}, {"index": 3, "codec_type": "subtitle", "codec_name": "mov_text"}]
    j = sub_probe(("eng", False, {}), ("eng", False, {"track_name": "Commentary"}), audio=(("eng", True),))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=srt_text([(1, 2, "Hello")] * 30), stderr=""))
    asked = []
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, starts=None, line=True, deep=False, streams=None: asked.append(sorted(items)) or
                        {k: {"verdict": "mismatch", "why": "5%", "timing": None} for k in items})
    for days, drop in ((0, []), (7, [(2, 2)])):
        settings(keep_days=days)
        info = {}
        assert hook.convert_subs(str(video), j, streams, [], talk.DURATION, True, str(tmp_path), info)[1] == drop, info
        assert info["tracks_unmatched"] == ["s1"] and ("tracks_kept_back" in info) == (days == 0)
    assert asked == [["s1"], ["s1"]]   # the commentary track is never checked


def test_a_conversion_asks_with_the_original_language(env, monkeypatch):
    """convert() passes the item's original language to the check, so a Japanese original holds a mismatch that its
    English dub gives, see sub_hold()."""
    mp4_import(env, monkeypatch, sidecars=())
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Japanese"}
    seen = []
    monkeypatch.setattr(hook, "convert_subs", lambda *a: seen.append(set(a[8])) or (a[3], []))
    monkeypatch.setattr(hook, "sub_on", lambda source, sub_check=False: True)
    hook.main([])
    assert seen == [hook.arr_decide.codes("Japanese")], seen


def test_sub_audio_takes_main_tagged_tracks_and_the_one_that_plays():
    ts = hook.arr_decide.classify(sub_probe(audio=(("eng", False), ("eng", True), ("spa", False), ("und", False))))
    assert hook.sub_audio(ts) == {"eng": 1, "spa": 2}   # the default English track, and no und track
    j = sub_probe(audio=(("eng", True), ("eng", False)))
    j["tracks"][2]["properties"]["track_name"] = "Commentary"
    assert hook.sub_audio(hook.arr_decide.classify(j)) == {"eng": 0}
    j = sub_probe(audio=(("eng", False), ("fre", True)))
    j["tracks"][1]["properties"]["track_name"] = "Commentary"   # the only English track is commentary
    assert hook.sub_audio(hook.arr_decide.classify(j)) == {"fra": 1}


def test_a_retime_keeps_the_mismatch_of_a_track_that_stays(env, monkeypatch):
    """KEEP_ORIGINALS_DAYS 0: the remux retimes s1, and s2 does not match. s2 stays in the file, loses its flags and
    keeps its alert, and the verdict cache marks nothing done that is not."""
    got = late_english_film(env, monkeypatch)
    env["probe"] = sub_probe(("eng", False, {}), ("eng", True, {"track_name": "SDH"}), audio=(("eng", True),))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=2.0), "s2": talk.cues(talk.OTHER)})
    hook.main([])
    rec = decided(env)
    (apply, fixes, drop), = got
    assert apply and list(fixes) == [2] and drop == [] and rec["subremux"]["codes"] == ["subtitle_retimed"], got
    assert "subtitle_audio_mismatch" in rec["reasons"] and rec["alert_kinds"] == ["submatch"], rec
    line = rec["findings"][0]["lines"][0]
    assert (line["code"], line["track"], line["gone"], line["kept_back"]) == ("stays", "s2", False, "keep_days"), line


def test_a_removal_moves_the_read_language_of_a_later_track(env, monkeypatch, tmp_path):
    """After s1 leaves the file, s2 is s1. Its read language moves with it, so the Romanian text tagged English still
    loses its flags."""
    got = removal_film(env, monkeypatch, tmp_path)
    env["probe"] = sub_probe(("eng", False, {}), ("eng", True, {"forced_track": True, "track_name": "Forced"}), audio=(("eng", True),))
    monkeypatch.setattr(hook, "subtitle_read", lambda path, j, want: {"s2": ("rum", 0.97, "97% rum")} if "s2" in want else {})
    hook.main([])
    rec = decided(env)
    assert got == [(True, {}, [2])] and rec["edits"] == [["track:=11", 0, 1], ["track:=11", 0, 1, hook.arr_decide.FORCED_FLAG]], rec
    assert "subtitle_text_muted" in rec["reasons"]


def test_a_flags_off_fallback_counts_as_pending_until_applied(env, monkeypatch, capsys):
    """KEEP_ORIGINALS_DAYS 0: a dry run plans the flags off, so its verdict asks for an action and the next run checks
    again. After the apply the file is done, and the next run skips it."""
    english_film(env, ("eng", True, {}), ("eng", False, {"track_name": "SDH"}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER), "s2": talk.cues(talk.RIGHT)})
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert not hook.sub_cached(env["path"])
    hook.main(["--backfill", "radarr", "--sub-check", "--apply"])
    assert [r for r in log_lines(env) if r.get("outcome")][-1]["outcome"] == "edited" and hook.sub_cached(env["path"])
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert len(env["words"]) == 2 and '"subtitles_cached": 1' in capsys.readouterr().out


def test_a_cue_moved_before_0_starts_at_0_and_no_stream_moves(sync_mkv, tmp_path):
    """A fix of 3 s moves the first cue, at 2 s, to -1 s. It starts at 0, as srt_moved() does, and the video and the
    audio keep their start times. A negative time would make ffmpeg move every stream."""
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    j = REAL_MKVMERGE(path)
    starts = {x["index"]: float(x.get("start_time") or 0) for x in hook.ff_streams(path)[1]}
    _, result, info = hook.resub(path, j, os.stat(path), True, {2: {"rate": "1/1", "offset": 3.0}})
    assert result == "subtitles remuxed", (result, info)
    assert {x["index"]: float(x.get("start_time") or 0) for x in hook.ff_streams(path)[1] if x["codec_type"] != "subtitle"} == \
        {i: t for i, t in starts.items() if i != 2}
    got = hook.subtitle_cues(path, REAL_MKVMERGE(path), {"s1"})["s1"]
    assert [(round(a, 2), round(b, 2)) for a, b, _ in got[:3]] == [(0.0, 0.5), (1.0, 2.5), (3.0, 4.5)], got[:3]


def test_the_proof_refuses_a_remux_that_moved_every_stream(sync_mkv, tmp_path):
    """Every stream 4 s later keeps each start against the video's. Only the absolute check sees it."""
    src, tmp = str(sync_mkv / "f.mkv"), str(tmp_path / "t.mkv")
    REAL_RUN(["ffmpeg", "-v", "error", "-itsoffset", "4", "-i", src, "-map", "0", "-c", "copy", "-copyinkf", tmp], check=True)
    assert proved(src, tmp, [], str(tmp_path))[0] is None
    assert "starts at 4." in proved(src, tmp, [], str(tmp_path), absolute=True)[0]


def test_resub_keeps_the_owner_and_mode_and_refuses_a_changed_original(sync_mkv, tmp_path, monkeypatch, settings):
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    os.chmod(path, 0o640)
    owned = []
    monkeypatch.setattr(hook.os, "chown", lambda p, uid, gid: owned.append((uid, gid)))
    st = os.stat(path)
    assert hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})[1] == "subtitles remuxed"
    assert owned == [(st.st_uid, st.st_gid)] and os.stat(path).st_mode & 0o7777 == 0o640
    # the app replaces the original while the remux runs: the original stays and the temp file goes
    st, real = os.stat(path), hook.mkvmerge
    monkeypatch.setattr(hook, "mkvmerge", lambda p: (os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9)), real(p))[1])
    settings(keep_days=7)   # the check comes before the keep, so nothing is kept
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    _, result, _ = hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})
    assert result.startswith("subtitle remux failed, the original changed: ") and os.stat(path).st_ino == st.st_ino
    assert not os.path.exists(os.path.join(tmp_path, hook.CFG.hide_dir))
    assert kept_files(tmp_path) == [] and not (tmp_path / ".kept").exists()


def test_resub_refuses_a_remux_whose_track_properties_did_not_come_back(sync_mkv, tmp_path, monkeypatch):
    """mkvpropedit exits 1 and sets nothing, so the new file keeps the UIDs ffmpeg wrote. props_fault() refuses it
    before the proof, and the original stays."""
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    before, real = open(path, "rb").read(), hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
                        if argv[0] == "mkvpropedit" else real(argv, **k))
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), os.stat(path), True, {2: {"rate": "1/1", "offset": 1.0}})
    assert result.startswith("subtitle remux failed: track 0 changed its ") and "proof" not in info, result
    assert open(path, "rb").read() == before and os.listdir(tmp_path) == ["f.mkv"]

def test_a_subtitle_remux_stopped_at_the_rename_removes_its_kept_link(sync_mkv, tmp_path, monkeypatch, settings):
    """SIGTERM at the rename leaves the original at its name, so its kept link goes and the temp file too."""
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    real, st = os.replace, os.stat(path)
    monkeypatch.setattr(hook.os, "replace", lambda a, b: (_ for _ in ()).throw(SystemExit(143)) if b == path else real(a, b))
    with pytest.raises(SystemExit):
        hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})
    assert os.stat(path).st_ino == st.st_ino and os.stat(path).st_nlink == 1 and kept_files(tmp_path) == []
    assert not os.path.exists(os.path.join(tmp_path, hook.CFG.hide_dir))


def test_limit_counts_a_remux_that_would_run(env, monkeypatch, tmp_path):
    """--limit stops after N files that need a change. A subtitle remux that a dry run would make is one."""
    got = late_english_film(env, monkeypatch)
    other = tmp_path / "media" / "Other.mkv"
    shutil.copy(env["path"], other)
    env["files"][str(other)] = copy.deepcopy(env["probe"])
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=i, title=f"Film {i}", movieFile={"id": i, "path": p})
                              for i, p in ((7, env["path"]), (8, str(other)))]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--sub-check", "--limit", "1"])
    assert len(got) == 1 and got[0][0] is False, got


def test_paths_refuses_a_scan(env, monkeypatch):
    for scan in ("--check-audio", "--check-video"):
        with pytest.raises(SystemExit) as ex:
            hook.main(["--backfill", "radarr", scan, "--paths", env["path"]])
        assert ex.value.code == 2


def test_a_language_check_runs_the_subtitle_hearings_with_its_model(env, monkeypatch, tmp_path):
    """The file's audio tag is in doubt, so the language check runs. It gets a file of the subtitle check's hearings,
    with the cues of the English track, so the model loads once. The file is gone afterwards."""
    env["movies"]["movie/7"].update(originalLanguage={"name": "Spanish"}, runtime=round(talk.DURATION / 60))
    env["probe"] = sub_probe(("eng", False, {}), audio=(("eng", True),))
    env["probe"]["tracks"][1]["properties"]["language_ietf"] = "es"   # the two tags disagree, so the language check runs
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False, stop=None: {"s1": talk.cues(talk.RIGHT)} if "s1" in want else {})
    seen = []

    def fake_lid_run(path, index, j, expect, timeout, fresh=False, keep=False, words=None, then=None, yield_to=None):
        if then:
            with open(then) as f:
                seen.append((keep, json.load(f)))
        return {"why": "no answer in these tests"}
    monkeypatch.setattr(hook, "lid_run", fake_lid_run)
    hook.main([])
    (keep, jobs), = seen
    assert keep and [(x["index"], x["lang"], len(x["cues"]), x["duration"]) for x in jobs] == [(0, "eng", talk.LINES, talk.DURATION)], jobs
    assert not [n for n in os.listdir(hook.CFG.state_dir) if n.startswith("subjobs-")]


@pytest.mark.parametrize("rate", [Fraction(25025, 24000), Fraction(24000, 25025)])
@pytest.mark.parametrize("chant", [False, True])
def test_a_ratio_hears_a_middle_window_where_its_speech_is(env, monkeypatch, rate, chant):
    """A subtitle timed for another frame rate: its cues drift 4 percent from the audio. The first hearing asks for a
    middle window near the centre between its windows. The densest cues there lie at cue times, and the fix to confirm
    puts their speech many seconds away in the audio, where the check hears it. When Whisper hears nothing there, as
    on a chant it drops, a window of THIRD seconds elsewhere in that part is heard in the same hearing. The windows
    then carry the fix."""
    S, stop = hook.arr_subsync, hook.arr_decide.STOPWORDS["eng"]
    english_film(env, ("eng", False, {}))
    track = talk.cues(talk.RIGHT, rate=rate)
    confirm = S.check(talk.heard(S.windows(track, talk.DURATION, stop)), track, "eng", talk.DURATION)["timing"]["confirm"]
    audio_mid = S.moved(S.windows(track, talk.DURATION, stop, parts=S.middle(confirm, talk.DURATION))[0] * 1000, confirm) / 1000
    quiet = lambda i: not (chant and audio_mid - 1 <= talk.FIRST + talk.GAP * i < audio_mid + S.WINDOW)
    hearing(env, monkeypatch, {"s1": track}, spoken=quiet)
    got = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: got.append(fixes) or ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    r = decided(env)["subcheck"]["s1"]
    (_, _, first), (_, _, mid) = env["words"]   # a drift of seconds needs no windows on the line, see arr_subsync.needs_line()
    assert len(first) == 2 and mid == [pytest.approx(audio_mid, abs=0.1)], (first, mid, audio_mid)
    assert 0.4 <= (mid[0] - first[0]) / (first[1] - first[0]) <= 0.6, (first, mid)
    assert bool(env["more"][1]) == chant, env["more"]   # the window of THIRD seconds, heard only when the first hears too little
    assert Fraction(r["timing"]["fix"]["rate"]) == rate and len(r["starts"]) == 2 and list(got[0].values())[0]["rate"] == r["timing"]["fix"]["rate"], r


def test_resub_holds_every_stream_to_its_own_start(sync_mkv, tmp_path, monkeypatch):
    """The remux must move no stream, so resub() asks the proof for the absolute start check."""
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    asked, real = [], hook.prove
    monkeypatch.setattr(hook, "prove", lambda *a, **k: asked.append(k) or real(*a, **k))
    assert hook.resub(path, REAL_MKVMERGE(path), os.stat(path), True, {2: {"rate": "1/1", "offset": 1.0}})[1] == "subtitles remuxed"
    assert asked[0]["absolute"] is True and asked[0]["retimed"] == {0: {"rate": "1/1", "offset": 1.0}}


def test_a_retime_before_0_fails_the_proof_when_the_audio_has_a_codec_delay(sync_mkv, tmp_path):
    """ffmpeg writes AAC into Matroska with a codec delay. A fix that moves the first cue before 0 starts it at 0, and
    the later cues then move by that delay. The proof refuses the remux, and the file stays as it was. A fix that moves
    no cue before 0 passes."""
    av, path = tmp_path / "av.mkv", str(tmp_path / "f.mkv")
    REAL_RUN(["ffmpeg", "-v", "error", "-i", str(sync_mkv / "av.mp4"), "-c:v", "copy", "-c:a", "aac", str(av)], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", path, str(av), "--language", "0:eng", str(sync_mkv / "s.srt")], check=True)
    j, st = REAL_MKVMERGE(path), os.stat(path)
    assert j["tracks"][1]["properties"].get("codec_delay"), j["tracks"][1]
    result = hook.resub(path, j, st, True, {2: {"rate": "1/1", "offset": 3.0}})[1]
    assert result.startswith("subtitle remux failed") and "moved" in result and os.stat(path).st_ino == st.st_ino, result
    assert hook.resub(path, j, st, True, {2: {"rate": "1/1", "offset": 1.0}})[1] == "subtitles remuxed"


def silent_late_windows(track):
    """spoken() for hearing(): no speech in the audio where the first hearing's late window and its third window lie,
    as a far drift can leave it."""
    S, stop = hook.arr_subsync, hook.arr_decide.STOPWORDS["eng"]
    first = S.windows(sorted(track), talk.DURATION, stop)
    late = (first[1], S.windows(sorted(track), talk.DURATION, stop, secs=S.THIRD, taken=first)[1])
    return lambda i: not any(a - 3 <= talk.FIRST + talk.GAP * i < a + S.THIRD for a in late)


def test_a_far_drift_hears_where_the_ratio_puts_the_speech(env, monkeypatch):
    """A 25 fps subtitle on a 23.976 video, and no speech where the late windows of the first hearing lie. The drift
    hearing hears where each far ratio puts the speech of the densest late cues. Those windows match, a middle window
    confirms the ratio, and the track gets its fix."""
    rate = Fraction(24000, 25025)
    english_film(env, ("eng", False, {}))
    track = talk.cues(talk.RIGHT, rate=rate)
    hearing(env, monkeypatch, {"s1": track}, spoken=silent_late_windows(track))
    got = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: got.append(fixes) or ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    r = decided(env)["subcheck"]["s1"]
    assert [len(ws) for _, _, ws in env["words"]] == [2, 2, 1], env["words"]   # the first hearing, the drift hearing, the middle
    assert r["verdict"] == "match" and Fraction(r["timing"]["fix"]["rate"]) == rate and got, r


LATE_CUT, VIDEO_END, HEADER_END = 139.2, 1432.7, 1561.9   # a live release: its subtitle runs 139.2 s late to 1561.9 s


def other_cut(env, monkeypatch, early="solid", end=VIDEO_END, shift=lambda t: 0, late_at=497, head=HEADER_END, tag=True,
              early_at=59, pair=False, passed=False):
    """A release whose only English subtitle comes from a cut with LATE_CUT more seconds at the start, so every cue sits
    that late. The video ends at end, and the header at the last cue, HEADER_END. The densest cues lie where they lay
    on the live file. The audio holds speech in two places only: the slower early drift window, and where the offset
    puts the speech of the first hearing's late window. early says what the drift window hears: "solid" every line,
    "eight" three short lines of MIN_WORDS words in all, "few" one line, "mixed" every line, the first three from
    another episode. shift(t) adds seconds to the cue of the line spoken at t, and None leaves the cue out. early_at and
    late_at are the first lines of the early and the late dense cues, head the header. tag False writes the file as
    ffmpeg does, with no DURATION tag, and the header probe reads the end from the last clusters. pair True also speaks
    two long lines where the fast ratio through the drift window at LATE_CUT puts the late window's speech. passed True
    gives the check the stream end, as the header step of an import does, and the header probe fails the test. Returns
    (the check's result, the first hearing's windows, the audio time of the late window's speech)."""
    S, stop = hook.arr_subsync, hook.arr_decide.STOPWORDS["eng"]
    dense = lambda i: early_at <= i < early_at + 4 or late_at <= i < late_at + 4   # by default the cues at 346 s and 1442 s
    long = lambda i, x: x[:-1] + " " + " ".join(talk.WORDS[i % 90:i % 90 + 5]) + "."
    lines = [long(i, x) if dense(i) else x for i, x in enumerate(talk.RIGHT + talk.script(3)[:145])]
    at = lambda i: talk.FIRST + talk.GAP * i
    cut = lambda ls: [c for i, c in enumerate(talk.cues(ls, offset=LATE_CUT, where=lambda i: shift(at(i)) or 0)) if shift(at(i)) is not None]
    track = cut(lines)
    first = S.windows(track, head, stop)
    slow, late = S.drift(first)[1], first[1] - LATE_CUT - shift(at(late_at))
    drift_lines = [i for i in range(len(lines)) if slow - 1 <= at(i) < slow + S.WINDOW]   # cue times past the first hearing's parts
    said = {"eight": drift_lines[1:4], "few": drift_lines[1:2]}.get(early, drift_lines)
    if early == "eight":
        for i, text in zip(said, ("Mira garden window.", "Tobin bicycle pancake.", "Juna lantern.")):
            lines[i] = text
        track = cut(lines)
    if pair:
        fast = S.drift(first[1:], (slow + S.WINDOW / 2, LATE_CUT), S.FAR[:1])[0]
        two = [i for i in range(len(lines)) if fast + 1 <= at(i) < fast + S.WINDOW - 3][:2]
        lines = [long(i, x) if i in two else x for i, x in enumerate(lines)]
        track, said = cut(lines), said + two
    heard = [talk.OTHER[i] if early == "mixed" and i in said[:3] else x for i, x in enumerate(lines)]
    hearing(env, monkeypatch, {}, lines=heard, spoken=lambda i: i in said or late - 1 <= at(i) < late + S.WINDOW)
    j = sub_probe(audio=(("eng", True),))
    j["container"]["properties"].update(duration=int(head * 1e9), writing_application="mkvmerge v92.0" if tag else "Lavf61.7.100")
    if tag:
        j["tracks"][0]["properties"]["tag_duration"] = f"00:{int(end // 60):02d}:{end % 60:012.9f}"   # the video's own end
    elif passed:
        monkeypatch.setattr(hook, "header_of", lambda p, j=None: pytest.fail("the caller passed the stream end"))
    else:
        monkeypatch.setattr(hook, "header_of", lambda p, j=None: dict(HEADER_HP, duration=head, video=end, audio=end, streams=end))
    return hook.sub_verdicts(env["path"], j, {"s1": ("eng", 0, track)}, streams=end if passed else None)["s1"], first, late


@pytest.mark.parametrize("end,early,tag,passed", [(VIDEO_END, "solid", True, False), (1320.0, "solid", True, False),
                                                  (VIDEO_END, "eight", True, False), (VIDEO_END, "solid", False, False),
                                                  (VIDEO_END, "solid", False, True)])
def test_a_subtitle_of_another_cut_is_heard_where_the_offset_puts_its_speech(env, monkeypatch, end, early, tag, passed):
    """The live case. A subtitle runs 139.2 s late through the whole file. The first hearing and the drift hearing hear
    too little, except one early drift window, which matches. Its offset puts the speech of the late cues 139.2 s
    before them, where one more hearing hears it. The track then matches with a fix of 139.2 s at 1/1. A drift window
    of MIN_WORDS words is enough, as it is for the verdict. No window of that hearing runs past the end of the video,
    so a shorter video leaves out the far window. With no DURATION tag, as on the live file, the header gives the
    track's own end, and the header probe gives the end of the video and the audio. An import passes the end that its
    header step read, and the check probes the header no more."""
    S = hook.arr_subsync
    r, first, late = other_cut(env, monkeypatch, early, end, tag=tag, passed=passed)
    assert r["windows"][3]["words"] == (S.MIN_WORDS if early == "eight" else 19), r["windows"]
    (_, _, a), _, (_, _, c) = env["words"]
    assert a == first and max(w["words"] for w in r["windows"][:2]) < S.MIN_WORDS, r["windows"]   # the first hearing hears too little
    assert any(abs(x - late) < 1 for x in c) and all(x + S.WINDOW <= end for x in c) and len(c) == (3 if end == VIDEO_END else 2), (late, c)
    assert r["verdict"] == "match" and r["timing"]["fix"]["rate"] == "1/1", r
    assert r["timing"]["fix"]["offset"] == pytest.approx(LATE_CUT, abs=0.1) and len(r["starts"]) == 3, r


@pytest.mark.parametrize("early", ["few", "mixed"])
def test_no_hearing_follows_the_drift_without_a_window_that_counts_toward_a_match(env, monkeypatch, early):
    """The early drift window hears one line, or lines of another episode among its own. Its matched words pass the
    drift step's hint, but the window does not count toward a match. So the check stops after the drift hearing,
    unknown, with no fix."""
    S = hook.arr_subsync
    r, _, _ = other_cut(env, monkeypatch, early)
    w = r["windows"][3]   # the slower early drift window
    assert w["overlap"] * w["words"] >= 3 and (w["words"] < S.MIN_WORDS if early == "few" else S.MISMATCH < w["overlap"] < S.MATCH), w
    assert len(env["words"]) == 2 and r["verdict"] == "unknown" and r["timing"] is None, (env["words"], r)


def test_a_late_track_whose_tail_is_in_time_gets_no_hearing_at_the_offset(env, monkeypatch):
    """The track runs LATE_CUT late up to 1200 s. The video then holds a scene with no speech that the track lacks, and
    the tail is in time. Both windows of the first hearing sit in the late part. A fix of LATE_CUT would move the tail
    early. The track ends inside the video, so the hearing at the offset never runs, and the check stays unknown."""
    scene = lambda t: None if 1200 - LATE_CUT <= t < 1200 else -LATE_CUT if t >= 1200 else 0
    r, first, _ = other_cut(env, monkeypatch, shift=scene, late_at=360, head=VIDEO_END)
    assert first[1] < 1200 and len(env["words"]) == 2 and r["verdict"] == "unknown" and r["timing"] is None, (first, env["words"], r)


def test_a_track_with_two_recap_blocks_gets_one_hearing_at_the_offset(env, monkeypatch):
    """A wrong track with two recap blocks: its cues sit LATE_CUT late up to 1000 s and twice that late after it. The
    drift window matches at LATE_CUT. Where that offset puts the late cues' speech, the audio holds none. The check
    makes one hearing at the offset at most, so it stops after three hearings, unknown."""
    r, _, _ = other_cut(env, monkeypatch, shift=lambda t: LATE_CUT if t >= 1000 else 0, head=1700.6)
    assert len(env["words"]) == 3 and r["verdict"] == "unknown" and r["timing"] is None, (env["words"], r)


def test_an_early_track_gets_no_hearing_at_the_offset(env, monkeypatch):
    """The track runs LATE_CUT early, and one stray cue at its end runs past the video. The drift window matches at
    -LATE_CUT. Only a late track gets the hearing at a matched window's offset, so the check stops after the drift
    hearing, unknown."""
    early = lambda t: None if t < 140 else 0 if t >= 1420 else -2 * LATE_CUT   # no cue before 0 s, and the last cue late
    r, _, _ = other_cut(env, monkeypatch, shift=early, early_at=159, late_at=516)
    w = r["windows"][3]   # the slower early drift window
    assert w["words"] >= 8 and w["offset"] == pytest.approx(-LATE_CUT, abs=0.5), w
    assert len(env["words"]) == 2 and r["verdict"] == "unknown" and r["timing"] is None, (env["words"], r)


def test_the_hearing_at_the_offset_runs_once_when_its_own_window_gives_a_new_hint(env, monkeypatch):
    """A track with two recap blocks: its cues sit LATE_CUT late up to 1000 s and twice that late after it. The drift
    window matches at LATE_CUT. The hearing at that offset hears two long lines at twice the offset, too few cues for a
    fix, and a longer window around them hears the same lines. That window gives a new hint, but the check makes one
    hearing at the offset at most. So it stops after four hearings, and the times stay."""
    r, _, _ = other_cut(env, monkeypatch, "eight", shift=lambda t: LATE_CUT if t >= 1000 else 0, head=1700.6, pair=True)
    w = r["windows"][-1]   # the longer window around the hearing at the offset
    assert w["words"] >= 8 and w["cues"] < 3 and w["offset"] == pytest.approx(2 * LATE_CUT, abs=1), r["windows"]
    assert [len(ws) for _, _, ws in env["words"][2:]] == [1, 1] and r["starts"][3][1] == hook.arr_subsync.THIRD, env["words"]
    assert len(env["words"]) == 4 and r["timing"]["fix"] is None, (env["words"], r)


@pytest.mark.parametrize("case", ["retimed", "remux fails", "repair failed", "other issue"])
def test_the_header_findings_follow_the_file_after_a_retime(env, monkeypatch, case):
    """The live case at the import. An ASS track runs LATE_CUT late to HEADER_END, past the video and the audio at
    VIDEO_END. The header step cannot cut it, and the header disagrees with the streams. The retime remux moves the
    track inside the video, and ffmpeg writes a header at its end. Then neither the overrun nor the duration alerts,
    and neither does a header repair that failed before. A new file with another header issue has no overrun either.
    When the remux fails, the overrun and the duration alert."""
    got = late_english_film(env, monkeypatch)
    fails = env["resub_fails"] = case == "remux fails"
    j = sub_probe(("eng", False, {"codec_id": "S_TEXT/ASS"}), audio=(("eng", True),))
    j["container"]["properties"].update(duration=int(HEADER_END * 1e9), writing_application="mkvmerge v92.0")
    for t in j["tracks"][:2]:
        t["properties"]["tag_duration"] = f"00:{int(VIDEO_END // 60):02d}:{VIDEO_END % 60:012.9f}"
    env["probe"], env["last_packet"], fake = j, VIDEO_END, hook.resub
    def resub(*a, **k):   # ffmpeg writes the header where the streams end, and its own writing application
        out = fake(*a, **k)
        if not fails:
            env["files"][env["path"]] = dict(j, container={"properties": dict(j["container"]["properties"], duration=int(VIDEO_END * 1e9),
                                                                              writing_application="Lavf61.7.103")})
        return out
    monkeypatch.setattr(hook, "resub", resub)
    header_issue(env, monkeypatch, repairable=case == "repair failed", result=("header_repair_failed", "header repair failed: mkvmerge exited 2"))
    hp = dict(HEADER_HP, duration=HEADER_END, streams=VIDEO_END, issue=["a subtitle event runs to 0:26:01, past the video and the audio at 0:23:52"],
              unfixable=[{"track": "s1", "codec": "S_TEXT/ASS", "end": HEADER_END, "streams": VIDEO_END}])
    clean = dict(HEADER_HP, duration=VIDEO_END, end=VIDEO_END, issue=["no usable Cues index: no Cues"] if case == "other issue" else [])
    monkeypatch.setattr(hook, "header_of", lambda p, j=None: copy.deepcopy(hp if hook.arr_decide.duration(j) > VIDEO_END + 1 else clean))
    ends = []   # the stream end the import passes to the subtitle check, so the check never probes the header again
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, *a: ends.append(a[-1]) or {k: {"verdict": "match", "why": "", "windows": [],
                                                                                                "timing": {"fix": {"rate": "1/1", "offset": LATE_CUT}, "why": "fit"}} for k in items})
    hook.main([])
    rec = decided(env)
    code = "header_repair_failed" if case == "repair failed" else "subtitle_overrun_unfixable"
    assert got and rec["header_repair"]["code"] == code and rec["subremux"]["done"] is not fails and ends == [VIDEO_END], (rec, ends)
    assert sorted(rec["alert_kinds"]) == (["duration", "subtiming", "subtitle"] if fails else []), rec["findings"]


def patched_windows(offset):
    """where() for talk.cues(): the cues around the two windows of the first hearing sit offset s off, and every other
    cue is in time, as a short patch of a right track that the windows happen to land on."""
    S, stop = hook.arr_subsync, hook.arr_decide.STOPWORDS["eng"]
    first = S.windows(talk.cues(talk.RIGHT), talk.DURATION, stop)
    return lambda i: offset if any(a - 5 <= talk.FIRST + talk.GAP * i <= a + S.WINDOW + 5 for a in first) else 0.0


@pytest.mark.parametrize("deep", [True, False])
def test_an_import_never_fixes_a_right_track_whose_two_windows_land_on_a_patch(env, monkeypatch, settings, deep):
    """The cues around both windows of the first hearing sit 0.85 s early, and the rest of the track is in time. The two
    windows ask for a fix under LINE_SHIFT, so the import hears a window at a third and one at two thirds of the file.
    Those sit in time, off the fix's line, so the times stay, and the deep analysis judges the track."""
    settings(subtitles="deep" if deep else "fix")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, where=patched_windows(-0.85))})
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("a patch is no reason to retime a track"))
    monkeypatch.setattr(hook, "deep_analysis", lambda n, pending, claimed=False: hook.drop_job(n, claimed))
    hook.main([])
    t = decided(env)["subcheck"]["s1"]["timing"]
    assert t["fix"] is None and t["unconfirmed"]["rate"] == "1/1" and len(env["words"]) == 2 and len(t["line"]) == 2, t
    assert "do not sit on its line" in t["why"] and t["why"].endswith("the deep analysis judges it" if deep else "the times stay"), t["why"]


def test_an_import_fixes_a_drift_whose_window_at_two_thirds_is_noisy(env, monkeypatch):
    """A 25/23.976 drift. Around two thirds of the file only 3 lines are heard, and their cues sit 0.48 s early. A fix
    that moves the cues by seconds keeps the rules of fit(), and the noisy window vetoes nothing, so the drift is fixed."""
    rate = Fraction(25025, 24000)
    region = lambda i: 0.62 * talk.DURATION <= talk.FIRST + talk.GAP * i <= 0.72 * talk.DURATION
    kept = [i for i in range(talk.LINES) if region(i)][::12][:3]
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, rate=rate, where=lambda i: -0.48 if region(i) else 0.0)},
            spoken=lambda i: not region(i) or i in kept)
    got = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: got.append(fixes) or ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    t = decided(env)["subcheck"]["s1"]["timing"]
    assert t["fix"] and Fraction(t["fix"]["rate"]) == rate and got, t


def test_an_import_fixes_a_small_shift_that_the_windows_on_its_line_confirm(env, monkeypatch):
    """A track 1.2 s late everywhere. The windows at a third and two thirds sit on the fix's line, so the fix stands."""
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=1.2)})
    got = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: got.append(fixes) or ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    t = decided(env)["subcheck"]["s1"]["timing"]
    assert near_fix(t["fix"], 1.2) and t["why"].endswith("sit on its line") and got and near_fix(list(got[0].values())[0], 1.2), t


def test_sub_time_hears_no_windows_on_the_line_as_its_sweep_judges_the_fix(env, monkeypatch):
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=1.2)})
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({"s1": sweep_rows([1.2] * 10)}, {"cpu": 0, "took": 0, "failed": []}))
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--sub-time", env["path"]])
    assert len(env["words"]) == 1 and near_fix(decided(env)["subcheck"]["s1"]["timing"]["fix"], 1.2), env["words"]


def test_the_windows_on_the_line_land_at_a_third_and_two_thirds_with_their_longer_windows(env, monkeypatch):
    """A track 1.2 s late. The hearing on the line asks for one window of WINDOW seconds in each part of LINE_PARTS,
    where the fix puts the speech, and a window of THIRD seconds in the same part for each."""
    S = hook.arr_subsync
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=1.2)})
    real, asked = hook.lid_run, []
    monkeypatch.setattr(hook, "lid_run", lambda *a, words=None, **k: asked.append(words) or real(*a, words=words, **k))
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    lang, ws, secs, more = asked[-1][:4]
    parts = [(a * talk.DURATION - 1.2, b * talk.DURATION) for a, b in S.LINE_PARTS]
    assert secs == S.WINDOW and len(ws) == len(more) == 2 and all(lo <= w <= hi and lo <= m <= hi for w, m, (lo, hi) in zip(ws, more, parts)), asked[-1]
    assert all(abs(w - m) >= S.WINDOW for w, m in zip(ws, more)) and decided(env)["subcheck"]["s1"]["timing"]["line"] == ws


def test_the_windows_on_the_line_are_placed_in_cue_time_and_heard_in_audio_time(env, monkeypatch):
    """A track 1.2 s late, and an earlier hearing at 373.3 s in the audio, which is 374.5 s in cue time. The windows on
    the line are picked from the cues in cue time, away from the windows heard already, each moved to cue time. A
    longer window of THIRD seconds goes in each part too. Both are heard where the fix puts their speech, 1.2 s
    earlier in the audio."""
    S, stop = hook.arr_subsync, hook.arr_decide.STOPWORDS["eng"]
    english_film(env, ("eng", False, {}))
    track = talk.cues(talk.RIGHT, offset=1.2)
    hearing(env, monkeypatch, {"s1": track})
    real, asked = hook.lid_run, []
    monkeypatch.setattr(hook, "lid_run", lambda *a, words=None, **k: asked.append(words) or real(*a, words=words, **k))
    first = S.windows(track, talk.DURATION, stop)
    r = hook.sub_verdicts(env["path"], env["probe"], {"s1": ("eng", 0, track)}, starts={0: [[first, S.WINDOW], [[373.3], S.WINDOW]]})["s1"]
    fix = r["timing"]["fix"]
    taken = [a + 1.2 for a in first + [373.3]]   # the windows heard, in cue time
    cue = S.windows(track, talk.DURATION, stop, taken=taken, parts=S.LINE_PARTS)
    long = S.windows(track, talk.DURATION, stop, secs=S.THIRD, taken=taken + cue, parts=S.LINE_PARTS)
    audio = lambda ws: [round(S.moved(a * 1000, fix) / 1000, 1) for a in ws]
    assert near_fix(fix, 1.2) and cue == [384.7, 726.8] and asked[-1][1:4] == (audio(cue), S.WINDOW, audio(long)), (asked[-1], cue, long)


def test_a_check_after_a_conversion_hears_the_windows_on_the_line_of_the_check_before(env, monkeypatch):
    """The second check replays the hearings of the first, the windows on the line too, and hears no new pair."""
    english_film(env, ("eng", False, {}))
    track = talk.cues(talk.RIGHT, offset=1.2)
    hearing(env, monkeypatch, {"s1": track})
    items = {"s1": ("eng", 0, track)}
    first = hook.sub_verdicts(env["path"], env["probe"], items)["s1"]
    env["words"].clear()
    again = hook.sub_verdicts(env["path"], env["probe"], items, starts={0: first["starts"]})["s1"]
    assert [w for _, _, w in env["words"]] == [s[0] for s in first["starts"]] and first["starts"][-1][3:] == ["line"], (env["words"], first["starts"])
    assert again["timing"]["fix"] == first["timing"]["fix"] and again["timing"]["line"] == first["timing"]["line"], again["timing"]


def test_a_backfill_never_promises_a_deep_analysis_it_does_not_queue(env, monkeypatch, settings):
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, where=patched_windows(-0.85))})
    rec = hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", round(talk.DURATION / 60), mode="sub_check", apply=False,
                                post=False))
    t = rec["subcheck"]["s1"]["timing"]
    assert t["fix"] is None and t["why"].endswith("so the times stay"), t["why"]


def test_a_replan_names_its_reason_in_the_log(env, monkeypatch):
    real, calls = hook.process, []

    def process(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise hook.Replan("a subtitle needs a remux", {"cpu": 1.0, "took": 1.0, "failed": [], "runs": 1, "cached": 0})
        return real(*a, **k)
    monkeypatch.setattr(hook, "process", process)
    hook.run_job(enqueue(env, 0, env["path"]), [], shared=True)
    warning = [r for r in log_lines(env) if r.get("result") == "warning"][0]
    assert warning["note"] == "re-planned with the file lock exclusive: a subtitle needs a remux", warning


@pytest.mark.parametrize("report", ["sub_time_report", "sub_text"])
def test_an_unconfirmed_fix_reads_as_times_stay_in_both_reports(report):
    r = {"verdict": "match", "why": "95%", "windows": [], "timing": {"fix": None, "unconfirmed": {"rate": "1/1", "offset": 0.85}, "why": "a fix of -0.85 s, but no"}}
    text = getattr(hook, report)({"result": "no change", "label": "f", "path": "/m/f.mkv", "subcheck": {"s1": r}}, "done")
    assert ("| times stay |" if report == "sub_time_report" else "(times stay: a fix of -0.85 s, but no)") in text, text


def test_the_sweep_judges_a_fix_only_by_the_windows_to_trust():
    """Windows with under 3 matched cues, or with no offset, say nothing about the fix."""
    rows = sweep_rows([1.2] * 5, lambda at: 1.2) + [dict(w, cues=2, off=2.0) for w in sweep_rows([0.0] * 5)] + \
        [dict(w, offset=None, off=1.5) for w in sweep_rows([0.0] * 5)]
    assert hook.sweep_confirms(rows) is None


def test_an_episode_the_app_lists_again_joins_the_unit_once(monkeypatch):
    monkeypatch.setattr(hook, "arr", lambda app, p: [{"id": 27, "episodeFileId": 70, "seriesId": 5, "runtime": 22}] * 2)
    out = hook.unit_files("sonarr", [], {"owner": "5", "file_id": "70", "episode_ids": "27", "path": "/tv/S01E02.mkv"})
    assert out[70]["items"] == [27] and [e["id"] for e in out[70]["eps"]] == [27], out


def test_each_file_of_a_radarr_download_names_its_own_movie(monkeypatch):
    """A Radarr download imported two films. Each file names its own movie as its owner, so a restore reads that movie."""
    movies = {"movie/7": {"runtime": 100, "movieFile": {"id": 70, "path": "/m/a.mkv"}}, "movie/8": {"runtime": 90, "movieFile": {"id": 80, "path": "/m/b.mkv"}}}
    monkeypatch.setattr(hook, "arr", lambda app, p: movies[p])
    out = hook.unit_files("radarr", [{"eventType": "downloadFolderImported", "movieId": 8, "data": {"fileId": "80"}}],
                          {"owner": "7", "file_id": "70", "path": "/m/a.mkv"})
    assert {fid: (f["owner"], f["runtime"]) for fid, f in out.items()} == {70: (7, 100), 80: (8, 90)}, out


def test_each_episode_of_a_sonarr_file_keeps_its_own_monitored_flag(monkeypatch):
    """A Sonarr file holds two episodes, and one is unmonitored. The read asks for both, gives each its own flag, and
    reads the file record once."""
    calls = []

    def fake_arr(app, p):
        calls.append(p)
        if p.startswith("episode?"):
            return [{"id": int(i), "episodeFileId": 41, "monitored": i == "31"} for i in parse_qs(p.split("?", 1)[1])["episodeIds"]]
        return {"episodefile/41": {"id": 41, "path": "/tv/S04E15E16.mkv"}}[p]
    monkeypatch.setattr(hook, "arr", fake_arr)
    assert hook.app_lists("sonarr", 5, {32: True, 31: True}) == ({41: "/tv/S04E15E16.mkv"}, {31: True, 32: False})
    assert hook.app_now("sonarr", 5, {31: True, 32: True}) == ["/tv/S04E15E16.mkv"] * 2 and calls.count("episodefile/41") == 2


@pytest.mark.parametrize("answer, linked, said", [
    ([{"id": 31, "episodeFileId": 41, "monitored": True}], False, "Sonarr lists ['/tv/x.mkv'], monitored [True, None]"),
    ([{"id": 31, "episodeFileId": 41, "monitored": True}, {"id": 32, "episodeFileId": 0, "monitored": True}], True, None)])
def test_a_sonarr_read_back_needs_every_episode_and_never_reads_file_0(monkeypatch, answer, linked, said):
    """The restored file holds two episodes. An episode the app leaves out of its answer is not monitored, so the file is
    not linked. An episode with file id 0 has no file, and the read never asks for file 0."""
    monkeypatch.setattr(hook, "arr", lambda app, p: answer if p.startswith("episode?") else {"episodefile/41": {"path": "/tv/x.mkv"}}[p])
    back = [{"result": "restored", "file_id": 9, "owner": 5, "old": "/tv/x.mkv"}]
    hook.read_back("sonarr", back, {9: {"items": [31, 32]}})
    assert (back[0]["linked"], back[0].get("read_back")) == (linked, said), back


def test_a_scan_with_ids_never_reads_the_file_of_another_movie(monkeypatch):
    """A scan with --ids lists the files of those movies only. A movie outside them whose movieFile join fails is never
    read, so it cannot stop the scan."""
    movies = [{"id": 7, "title": "Film A", "year": 1979, "movieFile": {"id": 11, "path": "/m/a.mkv"}},
              {"id": 8, "title": "Film B", "movieFileId": 99, "movieFile": {"id": 80, "path": "/m/b.mkv"}}]

    def fake_arr(app, p):
        if p == "moviefile/99":
            raise urllib.error.HTTPError(p, 500, "join", {}, None)
        return {"movie": movies, "qualityprofile": []}[p]
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook, "PROFILES", {})
    assert [(r[0], r[2]) for r in hook.library("radarr", {7})] == [(11, "Film A (1979)")]


def test_a_sonarr_grab_keeps_only_the_files_of_its_own_series(monkeypatch):
    """A grab names an episode id of another series. That series' file never joins the grab's links."""
    monkeypatch.setattr(hook, "arr", lambda app, p: {"episode?episodeIds=31&episodeIds=32": [{"id": 31, "episodeFileId": 41, "seriesId": 5},
                                                                                              {"id": 32, "episodeFileId": 42, "seriesId": 6}],
                                                      "episodefile/41": {"path": "/tv/a.mkv"}}[p])
    assert hook.ARR["sonarr"].grab_paths(5, [31, 32]) == ["/tv/a.mkv"]


def test_one_library_walk_serves_the_scan_the_backfill_and_sub_time(monkeypatch):
    """A scan labels an episode file with its series and file name. The count holds every file of the app, a series with
    no file and a film with no file too. --sub-time takes only the files at its paths."""
    eps = [dict(e, episodeFileId=f) for e, f in zip(SHOW_EPS, (21, 22))]
    series = [{"id": 5, "title": "Show D", "path": "/tv/Show D", "statistics": {"episodeFileCount": 2}},
              {"id": 6, "title": "Show E", "path": "/tv/Show E", "statistics": {"episodeFileCount": 0}}]
    files = [{"id": 21, "path": "/tv/Show D/S04E15.mkv", "relativePath": "Season 4/S04E15.mkv"},
             {"id": 22, "path": "/tv/Show D/S04E21.mkv", "relativePath": "Season 4/S04E21.mkv"}]
    movies = [{"id": 7, "title": "Film A", "year": 1979, "movieFile": {"id": 11, "path": "/m/a.mkv"}}, {"id": 8, "title": "Film B", "movieFile": None}]
    monkeypatch.setattr(hook, "arr", lambda app, p: {"series": series, "episode?seriesId=5": eps, "episodefile?seriesId=5": files, "movie": movies,
                                                      "qualityprofile": []}[p])
    monkeypatch.setattr(hook, "PROFILES", {})
    assert [(r[0], r[2]) for r in hook.library("sonarr", set())] == [(21, "Show D | S04E15.mkv"), (22, "Show D | S04E21.mkv")]
    assert [(r[0], r[2]) for r in hook.library("radarr", set())] == [(11, "Film A (1979)")]
    assert hook.ARR["sonarr"].library(series)[1] == 2 and hook.ARR["radarr"].library(movies)[1] == 1
    assert [f["id"] for f, _ in hook.ARR["sonarr"].library(series, paths=["/tv/Show D/S04E21.mkv"])[0]] == [22]


def test_a_slow_trend_of_a_right_track_gets_no_fix(env, monkeypatch):
    """A film of 52 minutes whose cues slide from 0.1 s to 0.85 s early, a trend no frame-rate ratio explains. The line
    through the two windows runs past MIN_SHIFT at the file's end, but a plain shift under MIN_SHIFT is all a fix could
    do, and it would move the early part off. So the times stay."""
    lines = talk.script(1) + talk.script(5) + talk.script(6)
    dur = talk.FIRST + talk.GAP * len(lines) + 60
    english_film(env, ("eng", False, {}))
    env["probe"]["container"]["properties"]["duration"] = int(dur * 1e9)
    env["movies"]["movie/7"]["runtime"] = round(dur / 60)
    at = lambda i: talk.FIRST + talk.GAP * i
    hearing(env, monkeypatch, {"s1": talk.cues(lines, where=lambda i: -0.1 - 0.75 * at(i) / dur)}, lines=lines)
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("a slow trend is no reason to retime a track"))
    hook.main([])
    t = decided(env)["subcheck"]["s1"]["timing"]
    assert t["fix"] is None and t["why"] == "in time" and len(env["words"]) == 1, t


def test_only_the_first_hearing_names_a_mismatch(env, monkeypatch):
    """Near miss: a track with none of the audio's words, and no speech where the late windows of the first
    hearing lie. The drift windows read a mismatch, but a window away from the densest cues is no proof, so the verdict stays
    unknown and the track stays."""
    english_film(env, ("eng", False, {}))
    track = talk.cues([f"zz{i}a zz{i}b zz{i}c zz{i}d" for i in range(talk.LINES)])
    hearing(env, monkeypatch, {"s1": track}, spoken=silent_late_windows(track))
    got = []
    monkeypatch.setattr(hook, "resub", lambda *a, **k: got.append(a) or ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    r = decided(env)["subcheck"]["s1"]
    assert len(env["words"]) == 2 and r["verdict"] == "unknown" and "only in windows heard after the first" in r["why"] and not got, r


def test_a_window_with_too_few_matched_cues_hears_a_longer_window(env, monkeypatch):
    """A track 2 s late, and only two whole lines in the early window: two anchors, under MIN_CUES. A window of THIRD
    seconds around it holds more lines, and the track gets its fix."""
    english_film(env, ("eng", False, {}))
    track = talk.cues(talk.RIGHT, offset=2.0)
    early = hook.arr_subsync.windows(sorted(track), talk.DURATION, hook.arr_decide.STOPWORDS["eng"])[0]
    inside = [i for i in range(talk.LINES) if early <= talk.FIRST + talk.GAP * i < early + hook.arr_subsync.WINDOW]
    hearing(env, monkeypatch, {"s1": track}, spoken=lambda i: i not in inside[2:])
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    r = decided(env)["subcheck"]["s1"]
    assert [ws for _, _, ws in env["words"]][1] == [round(early - (hook.arr_subsync.THIRD - hook.arr_subsync.WINDOW) / 2, 1)], env["words"]
    assert r["timing"]["fix"] and r["timing"]["fix"]["rate"] == "1/1" and abs(r["timing"]["fix"]["offset"] - 2.0) < 0.1, r


@pytest.mark.parametrize("step, alerts", [(0.99, False), (1.0, True)])
def test_a_piecewise_step_under_a_second_goes_to_the_report_only(step, alerts, monkeypatch):
    """The windows of a right track can differ by most of a second. A piecewise result alerts only from STEP_ALERT, 1 s,
    on. Below that the backfill report still names it, and it never gets a fix."""
    t = {"fix": None, "piecewise": True, "offsets": [0.23, round(0.23 + step, 2)], "why": "the cues are off, which no frame-rate ratio explains"}
    sync = {"s1": {"verdict": "match", "why": "the heard words match", "windows": [], "timing": t}}
    assert [f["kind"] for f in hook.sub_findings({}, sync, [])] == (["subtiming"] if alerts else [])
    assert "times stay: the cues are off" in hook.sub_text({"subcheck": sync}, "done")


# --- --sub-time: picture tracks, flash cues and the reference timing (docs/design.md, "Subtitle match") -------------

def pgs_sup(cues, fade=()):
    """A PGS .sup file that shows a small box for each cue (start, end) and clears it at the end. A cue whose index is
    in fade gets a palette update halfway through, as a fade does."""
    seg = lambda t, kind, data: b"PG" + struct.pack(">II", round(t * 90000), 0) + bytes([kind]) + struct.pack(">H", len(data)) + data
    pcs = lambda number, state, update, objects: struct.pack(">HHBHBBBB", 160, 90, 0x10, number, state, update, 0, objects) \
        + (struct.pack(">HBBHH", 0, 0, 0, 10, 10) if objects else b"")
    wds, rle = bytes([1, 0]) + struct.pack(">HHHH", 10, 10, 4, 2), (b"\x01" * 4 + b"\x00\x00") * 2
    ods = struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big") + struct.pack(">HH", 4, 2) + rle
    out = b""
    for n, (a, b) in enumerate(cues):
        out += seg(a, 0x16, pcs(3 * n, 0x80, 0, 1)) + seg(a, 0x17, wds) + seg(a, 0x14, bytes([0, 0, 1, 235, 128, 128, 255])) + seg(a, 0x15, ods) \
            + seg(a, 0x80, b"")
        if n in fade:
            out += seg((a + b) / 2, 0x16, pcs(3 * n + 1, 0, 1, 1)) + seg((a + b) / 2, 0x14, bytes([0, 1, 1, 128, 128, 128, 128])) + seg((a + b) / 2, 0x80, b"")
        out += seg(b, 0x16, pcs(3 * n + 2, 0, 0, 0)) + seg(b, 0x17, wds) + seg(b, 0x80, b"")
    return out


PIC_CUES = [(2.0 * i, 2.0 * i + 1.5) for i in range(1, 19)]


def flash_srt(n=20):
    """A SubRip text of n written lines whose cues each show 0.138 s, one every 2 s, with CRLF line breaks inside some."""
    t = lambda x: f"00:00:{int(x):02d},{round(x % 1 * 1000):03d}"
    return "".join(f"{i}\r\n{t(2 * i)} --> {t(2 * i + 0.138)}\r\n" + (f"Line {i}\r\nand more {i}" if i % 3 == 0 else f"Short {i}") + "\r\n\r\n"
                   for i in range(1, n + 1))


@pytest.fixture(scope="module")
def pic_mkv(tmp_path_factory):
    """Matroska files of 40 seconds: pgs.mkv holds a SubRip track and a PGS track of PIC_CUES, the third cue with a
    palette update. vob.mkv holds the same cues as VobSub, which ffmpeg makes from the PGS track. flash.mkv holds a
    SubRip and an ASS track whose cues each show a seventh of a second."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge") and shutil.which("mkvextract")):
        pytest.skip("needs ffmpeg and mkvtoolnix")
    d = tmp_path_factory.mktemp("pic")
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=40:size=160x90:rate=25", "-f", "lavfi", "-i",
              "sine=frequency=440:duration=40", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac", str(d / "av.mp4")], check=True)
    (d / "t.sup").write_bytes(pgs_sup(PIC_CUES, fade={2}))
    (d / "s.srt").write_text("".join(f"{i}\n00:00:{2 * i:02d},000 --> 00:00:{2 * i + 1:02d},500\n{talk.RIGHT[i]}\n\n" for i in range(1, 19)))
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "pgs.mkv"), str(d / "av.mp4"), "--language", "0:eng", str(d / "s.srt"), "--language", "0:spa", str(d / "t.sup")],
             check=True)
    (d / "plain.sup").write_bytes(pgs_sup(PIC_CUES))   # ffmpeg would turn a palette update into a cue of its own
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "plain.mkv"), str(d / "av.mp4"), str(d / "plain.sup")], check=True)
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-fix_sub_duration", "-i", str(d / "plain.mkv"), "-map", "0", "-c:v", "copy", "-c:a", "copy", "-c:s", "dvdsub",
              str(d / "vob.mkv")], check=True)
    (d / "f.srt").write_bytes(flash_srt().encode())
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,28,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,"
            "100,0,0,1,2,1,2,10,10,10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    (d / "f.ass").write_text(head + "".join(f"Dialogue: 0,0:00:{2 * i:02d}.00,0:00:{2 * i:02d}.14,Default,,0,0,0,,{{\\i1}}Line {i}{{\\i0}}\\Nnext\n"
                                                   for i in range(1, 21)))
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "flash.mkv"), str(d / "av.mp4"), "--language", "0:eng", str(d / "f.srt"), "--language", "0:eng",
              "--track-name", "0:Styled", str(d / "f.ass")], check=True)
    return d


def test_picture_cues_read_pgs_display_sets_and_vobsub_stops(pic_mkv):
    """A PGS set with an object starts a cue and the next set ends it, while a palette update keeps it. A VobSub cue
    ends at the stop command of its packet."""
    path = str(pic_mkv / "pgs.mkv")
    got = hook.picture_cues(path, REAL_MKVMERGE(path), {"s1", "s2"})
    assert list(got) == ["s2"] and [(a, b) for a, b, _ in got["s2"]] == PIC_CUES, got
    path = str(pic_mkv / "vob.mkv")
    got = hook.picture_cues(path, REAL_MKVMERGE(path), {"s1"})["s1"]
    stop = 131 * 1024 / 90000   # ffmpeg's stop command for 1.5 s, in units of 1024/90000 s. The BlockDuration says 1.5 s.
    assert [a for a, _, _ in got] == [a for a, _ in PIC_CUES] and all(abs(b - a - stop) < 0.001 for a, b, _ in got), got[:3]


def test_a_picture_cue_ends_at_span(pic_mkv, tmp_path):
    """A PGS cue that stays on screen for 30 s counts SPAN seconds, so a long sign never outweighs the dialogue."""
    (tmp_path / "long.sup").write_bytes(pgs_sup([(2.0, 32.0), (33.0, 34.5)]))
    path = str(tmp_path / "long.mkv")
    REAL_RUN(["mkvmerge", "-q", "-o", path, str(pic_mkv / "av.mp4"), str(tmp_path / "long.sup")], check=True)
    assert [(a, b) for a, b, _ in hook.picture_cues(path, REAL_MKVMERGE(path), {"s1"})["s1"]] == [(2.0, 2.0 + hook.arr_subsync.SPAN), (33.0, 34.5)]


def test_spu_stop_reads_the_stop_command_and_the_cue_ends_at_span():
    """A control sequence at 0x10 holds its time, 131 units of 1024/90000 s, a colour command and the stop. A packet
    with no stop names nothing."""
    spu = b"\x00\x20\x00\x10" + bytes(12) + (131).to_bytes(2, "big") + b"\x00\x10" + b"\x03\x11\x11" + b"\x02\xff"
    assert hook.spu_stop(spu) == pytest.approx(131 * 1024 / 90000) and hook.spu_stop(b"\x00\x08\x00\x04\x00\x00\x00\x04\xff") is None


def test_block_head_reads_the_start_of_a_cut_block():
    d = hook.arr_decide
    block = cue_el(d.SIMPLEBLOCK, bytes([0x82]) + b"\x00\x10\x80" + b"\x16\x00\x0b" + bytes(11))
    assert d.block_head(block[:12], 2) == (b"\x16\x00\x0b" + bytes(3), (16, None))
    assert d.block_head(block, 3) == (None, None) and d.block_head(cue_el(d.BLOCKGROUP, block), 2)[1] == (16, None)


@pytest.mark.parametrize("name, codec", [("pgs.mkv", "S_HDMV/PGS"), ("vob.mkv", "S_VOBSUB")])
def test_resub_retimes_a_picture_track_through_the_proof(pic_mkv, tmp_path, monkeypatch, settings, name, codec):
    """ffmpeg copies a PGS or VobSub track with -itsoffset and -itsscale, and the proof shows every packet the same."""
    path = str(tmp_path / name)
    shutil.copy(pic_mkv / name, path)
    settings(keep_days=0)
    j = REAL_MKVMERGE(path)
    tid = next(t["id"] for t in j["tracks"] if t["properties"].get("codec_id") == codec)
    for fix in ({"rate": "1/1", "offset": 2.0}, {"rate": "25025/24000", "offset": 0.0}):
        j = REAL_MKVMERGE(path)
        _, result, info = hook.resub(path, j, os.stat(path), True, {tid: fix})
        assert result == "subtitles remuxed" and all(e["match"] for e in info["proof"]), (result, info)
    pos = f"s{[t['id'] for t in j['tracks'] if t['type'] == 'subtitles'].index(tid) + 1}"
    got = hook.picture_cues(path, REAL_MKVMERGE(path), {pos})[pos]
    want = [max(0, hook.arr_subsync.moved(hook.arr_subsync.moved(a * 1000, {"rate": "1/1", "offset": 2.0}), {"rate": "25025/24000", "offset": 0.0})) / 1000
            for a, _ in PIC_CUES]
    assert len(got) == 18 and all(abs(a - w) <= 0.003 for (a, _, _), w in zip(got, want)), (got[:3], want[:3])


def test_flash_check_finds_the_flash_tracks_by_their_cue_durations(pic_mkv, monkeypatch):
    """The Cues give each block's duration, so a track whose median entry is long is never read in full."""
    path = str(pic_mkv / "flash.mkv")
    plans = hook.flash_check(path, REAL_MKVMERGE(path), [])
    assert sorted(plans) == ["s1", "s2"] and plans["s1"][0][2:] == (2.138, 3.917) and plans["s2"][0][2:] == (2.14, 3.92), plans["s1"][:2]
    assert hook.cue_lengths(path, REAL_MKVMERGE(path), {"s1"}) == {"s1": [0.138] * 20}
    path, read = str(pic_mkv / "pgs.mkv"), []
    monkeypatch.setattr(hook, "subtitle_cues", lambda *a: read.append(a) or {})
    assert hook.flash_check(path, REAL_MKVMERGE(path), []) == {} and read == []


def test_resub_lengthens_flash_ends_and_keeps_text_starts_and_the_ass_header(pic_mkv, tmp_path, monkeypatch, settings):
    """The new ends go in through mkvextract, the text and mkvmerge. Each packet keeps its bytes and start, the ASS
    header stays, and the proof holds each end to the plan. A retime of the same track, by a ratio too, moves the new
    ends with it."""
    path = str(tmp_path / "flash.mkv")
    shutil.copy(pic_mkv / "flash.mkv", path)
    settings(keep_days=0)
    j = REAL_MKVMERGE(path)
    plans = hook.flash_check(path, j, [])
    fix = {"rate": "25025/24000", "offset": 1.0}
    _, result, info = hook.resub(path, j, os.stat(path), True, {2: fix}, (), {2: plans["s1"], 3: plans["s2"]})
    assert result == "subtitles remuxed" and [(e["stream"], e.get("ended")) for e in info["proof"] if e.get("ended")] == [("subtitle 2", 20), ("subtitle 3", 20)]
    new = REAL_MKVMERGE(path)
    assert new["tracks"][3]["properties"]["codec_private_data"] == j["tracks"][3]["properties"]["codec_private_data"]
    assert [t["properties"].get("track_name") for t in new["tracks"]] == [t["properties"].get("track_name") for t in j["tracks"]]
    got = hook.subtitle_cues(path, new, {"s1", "s2"})
    at = lambda t: hook.arr_subsync.moved(t * 1000, fix) / 1000
    assert got["s1"][2][0] == pytest.approx(at(6.0), abs=0.002) and got["s1"][2][1:] == (pytest.approx(at(7.917), abs=0.002), "Line 3\r\nand more 3"), got["s1"][:3]
    assert got["s2"][0] == (2.0, 3.92, "{\\i1}Line 1{\\i0}\\Nnext"), got["s2"][:1]


@pytest.mark.parametrize("tid", [2, 3])
def test_resub_moves_a_recap_of_several_cues_before_the_start(pic_mkv, tmp_path, settings, tid):
    """A subtitle of another cut opens with a recap. The fix moves two cues wholly before 0 and one across it, on a
    SubRip and on an ASS track. Each cue wholly before 0 keeps a length of 1 ms at 0, the cue across 0 ends where the
    fix moves its end, and the proof passes."""
    path = str(tmp_path / "flash.mkv")
    shutil.copy(pic_mkv / "flash.mkv", path)
    settings(keep_days=0)
    pos = f"s{tid - 1}"
    was = hook.subtitle_cues(path, REAL_MKVMERGE(path), {pos})[pos]
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), os.stat(path), True, {tid: {"rate": "1/1", "offset": 6.1}})
    assert result == "subtitles remuxed" and all(e["match"] for e in info["proof"]), (result, info)
    got = hook.subtitle_cues(path, REAL_MKVMERGE(path), {pos})[pos]
    want = [(max(0, a - 6.1), max(0.001, b - 6.1)) for a, b, _ in was]
    assert len(got) == 20 and all(abs(a - x) + abs(b - y) <= 0.002 for (a, b, _), (x, y) in zip(got, want)), (got[:4], want[:4])
    assert [b for _, b, _ in got[:2]] == [0.001, 0.001], got[:2]


def test_a_retimed_sidecar_with_a_recap_passes_the_conversion_proof(pic_mkv, tmp_path):
    """srt_moved() gives each cue that ends before 0 a length of 1 ms. mkvmerge drops a cue of length 0, and the
    proof of the conversion that muxes the sidecar then refused the new file."""
    text = "".join(f"{i}\n00:00:{2 * i:02d},000 --> 00:00:{2 * i:02d},500\nLine {i}\n\n" for i in range(1, 6))
    mux, new = tmp_path / "m.srt", str(tmp_path / "c.mkv")
    mux.write_text(hook.srt_moved(text, {"rate": "1/1", "offset": 6.1}))
    assert mux.read_text().count("00:00:00,000 --> 00:00:00,001") == 2
    REAL_RUN(["mkvmerge", "-q", "--disable-lacing", "-o", new, str(pic_mkv / "av.mp4"), str(mux)], check=True)
    assert proved(str(pic_mkv / "av.mp4"), new, [{"name": "m.srt", "mux": str(mux), "charset": "UTF-8"}], str(tmp_path))[0] is None


def test_the_proof_holds_each_end_to_the_plan(pic_mkv, tmp_path):
    src = str(pic_mkv / "flash.mkv")
    plan = [e for _, _, _, e in hook.flash_check(src, REAL_MKVMERGE(src), [])["s1"]]
    stream, why = hook.prove(src, src, [], str(tmp_path), ended={0: plan})[0]   # a cue end is no packet refusal, so no stream
    assert why.startswith("stream subtitle 2 ends cue 1 at 2.138 s, and the plan at 3.917 s"), why
    assert stream is None
    assert proved(src, src, [], str(tmp_path), ended={0: [2 * i + 0.138 for i in range(1, 21)]})[0] is None
    assert "ends 20 cues, and the plan 19" in proved(src, src, [], str(tmp_path), ended={0: [2 * i + 0.138 for i in range(1, 20)]})[0]


def test_set_ends_pairs_each_cue_or_refuses():
    plan = [(2.0, "a", 2.1, 3.0), (4.0, "b", 4.1, 5.5)]
    text = "1\n00:00:02,000 --> 00:00:02,100\na\n\n2\n00:00:04,000 --> 00:00:04,100\nb\n"
    assert hook.set_ends(text, False, plan) == text.replace("00:00:02,100", "00:00:03,000").replace("00:00:04,100", "00:00:05,500")
    with pytest.raises(RuntimeError, match="the plan's at 4.0"):
        hook.set_ends(text.replace("00:00:04,000", "00:00:04,500"), False, plan)
    with pytest.raises(RuntimeError, match="holds 1 cues, and the plan 2"):
        hook.set_ends(text.split("\n\n")[0], False, plan)
    ass = "Dialogue: 0,0:00:04.00,0:00:04.10,Default,,0,0,0,,b\nDialogue: 0,0:00:02.00,0:00:02.10,Default,,0,0,0,,a\n"
    assert hook.set_ends(ass, True, plan) == ass.replace("0:00:04.10", "0:00:05.50").replace("0:00:02.10", "0:00:03.00")
    with pytest.raises(KeyError):
        hook.set_ends(ass.replace(",,b", ",,c"), True, plan)
    # two events of one start and text, as two layers of a line: each takes its own end, in the order of the text
    twin = "Dialogue: 0,0:00:02.00,0:00:02.10,Default,,0,0,0,,a\nDialogue: 1,0:00:02.00,0:00:09.00,Default,,0,0,0,,a\n"
    assert hook.set_ends(twin, True, [(2.0, "a", 2.1, 3.0), (2.0, "a", 9.0, 9.0)]) == twin.replace("0:00:02.10", "0:00:03.00")


def test_ended_track_refuses_a_mkvextract_warning(pic_mkv, tmp_path, monkeypatch):
    """mkvextract exits 1 on a warning. A fix never builds on one."""
    src = str(pic_mkv / "flash.mkv")
    j = REAL_MKVMERGE(src)
    plan = hook.flash_check(src, j, [])["s1"]
    real = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **k: types.SimpleNamespace(returncode=1, stdout="Warning: a gap", stderr="")
                        if "mkvextract" in argv and real(argv, **k) else real(argv, **k))
    with pytest.raises(RuntimeError, match="mkvextract exited 1: Warning: a gap"):
        hook.ended_track(src, j, next(t["id"] for t in j["tracks"] if t["type"] == "subtitles"), plan, str(tmp_path))


def test_timed_ends_a_cue_with_no_duration_at_the_next_cue_or_after_5_s():
    assert hook.timed([(1.0, None, "a"), (3.0, None, "b"), (20.0, 0.5, "c"), (30.0, None, "d")]) == \
        [(1.0, 3.0, "a"), (3.0, 8.0, "b"), (20.0, 20.5, "c"), (30.0, 35.0, "d")]


def test_subtitles_check_reports_a_flash_sidecar_and_leaves_it(env, monkeypatch, settings, tmp_path):
    english_film(env)
    side = env["path"][:-4] + ".fr.srt"
    with open(side, "w", newline="") as f:
        f.write(flash_srt())
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    settings(subtitles="check")
    hook.main([])
    rec = decided(env)
    (e,) = rec["sidecars"]
    assert (e["action"], e["result"], e["left"]) == ("lengthen", "left", "SUBTITLES is set to check"), rec["sidecars"]
    assert rec["alert_kinds"] == ["subtiming"] and open(side, newline="").read() == flash_srt() and not os.path.exists(tmp_path / ".kept")


def test_a_flash_sidecar_is_rewritten_and_its_original_kept(env, monkeypatch, settings, tmp_path):
    """A sidecar in any language, forced too, whose cues flash gets new ends. The original is kept, the fix goes to the
    log only, and with KEEP_ORIGINALS_DAYS 0 it stays as it was with an alert."""
    english_film(env)
    side = env["path"][:-4] + ".fr.forced.srt"
    with open(side, "w", newline="") as f:
        f.write(flash_srt())
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    hook.main([])
    rec = decided(env)
    assert rec["flash"][os.path.basename(side)]["lengthened"] == 20 and rec["sidecars"][0]["result"] == "retimed" and rec["alert_kinds"] == [], rec
    assert "00:00:02,000 --> 00:00:03,917\nShort 1" in open(side).read()
    kept = rec["sidecars"][0]["kept"]
    assert open(kept, newline="").read() == flash_srt()
    with open(side, "w", newline="") as f:
        f.write(flash_srt())
    settings(keep_days=0)
    hook.main([])
    rec = [r for r in log_lines(env) if r.get("outcome")][-1]
    assert rec["sidecars"][0]["result"] == "left" and rec["alert_kinds"] == ["subtiming"] and open(side, newline="").read() == flash_srt(), rec


def flash_film(env, monkeypatch, fails=False):
    """An English film with a French SubRip track whose cues flash, and a fake remux that records its call."""
    english_film(env, ("fre", False, {}))
    cs = [(10.0 + 3 * i, 10.138 + 3 * i, f"ligne {i}") for i in range(40)]
    monkeypatch.setattr(hook, "cue_lengths", lambda path, j, want, full=False, stop=None: {"s1": [0.138] * 40} if "s1" in want else {})
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False, stop=None: {"s1": cs} if "s1" in want else {})
    got = []

    def fake_resub(path, j, st, apply, fixes, drop=(), ends=None):
        got.append((apply, fixes, list(drop), ends))
        return ("subtitle_remux_failed", "subtitle remux failed: the packet data of stream audio 1 (aac) differ", {"warnings": None}) if fails else ("subtitles_remuxed", "subtitles remuxed", {"warnings": None})
    monkeypatch.setattr(hook, "resub", fake_resub)
    return got


def test_an_import_lengthens_flash_ends_in_a_remux_and_logs_it_only(env, monkeypatch):
    """A track in any language whose cues flash gets new ends in the remux. The fix goes to the log, with no alert."""
    got = flash_film(env, monkeypatch)
    hook.main([])
    rec = decided(env)
    (apply, fixes, drop, ends), = got
    assert apply and fixes == {} and drop == [] and list(ends) == [2] and ends[2][0] == (10.0, "ligne 0", 10.138, 12.917), ends
    assert rec["flash"]["s1"]["lengthened"] == 40 and rec["reasons"][0] == "subtitle_ends_lengthened" and rec["alert_kinds"] == [], rec


def test_subtitles_check_keeps_flash_ends_and_alerts(env, monkeypatch, settings):
    got = flash_film(env, monkeypatch)
    settings(subtitles="check")
    hook.main([])
    rec = decided(env)
    assert got == [] and rec["alert_kinds"] == ["subtiming"], rec
    assert [(x["code"], x["track"]) for x in rec["findings"][0]["lines"]] == [("check_flash", "s1")], rec["findings"]


def test_a_failed_flash_fix_alerts(env, monkeypatch):
    flash_film(env, monkeypatch, fails=True)
    hook.main([])
    rec = decided(env)
    line = rec["findings"][0]["lines"][0]
    assert rec["alert_kinds"] == ["subtiming"] and rec["apply"] and (line["code"], line["tracks"]) == ("not_retimed", ["s1"]), rec["findings"]
    assert line["result"].startswith("subtitle remux failed"), line


def test_flash_check_skips_a_track_whose_read_came_short(env, monkeypatch):
    """The Cues list 41 entries and the read gets 40 cues: a plan for part of the track never runs."""
    got = flash_film(env, monkeypatch)
    monkeypatch.setattr(hook, "cue_lengths", lambda path, j, want, full=False, stop=None: {"s1": [0.138] * 41})
    hook.main([])
    assert got == [] and "flash" not in decided(env)


# --- the --sub-time command ------------------------------------------------------------------------------------------

REF = talk.show(11, n=170)   # a reference whose words matched, in audio time, within talk.DURATION
IN_TIME = {"verdict": "match", "why": "the heard words match the cues at 90%, 92%", "windows": [], "timing": {"fix": None, "why": "in time"}}


def sub_time_film(env, monkeypatch, subs, cues, sync=None, pictures=None, sides=None):
    """An English film for --sub-time with the subtitles subs of sub_probe(), their cues {position: cues}, the word
    check's results sync (s1 in time by default), picture cues and sidecars {suffix: cues}. The hearing and the sweep
    are faked, and the remux records its calls."""
    english_film(env, *subs)
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    for ext, cs in (sides or {}).items():
        with open(env["path"][:-4] + ext, "w") as f:
            f.write(srt_text(cs))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False, stop=None: {p: c for p, c in cues.items() if p in want})
    monkeypatch.setattr(hook, "picture_cues", lambda path, j, want, full=False, stop=None: {p: c for p, c in (pictures or {}).items() if p in want})
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, starts=None, line=True, deep=False, streams=None: {k: dict(v, audio=0, starts=[]) for k, v in (sync or {"s1": IN_TIME}).items()
                                                                                   if k in items})
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({}, {"cpu": 0, "took": 0, "failed": []}))
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    got = []

    def fake_resub(path, j, st, apply, fixes, drop=(), ends=None):
        got.append((apply, fixes, list(drop)))
        return ("subtitles_remuxed", "subtitles remuxed", {"warnings": None, "kept": "/kept/f.mkv"}) if apply else ("would_remux_subtitles", "would remux subtitles: x", {})
    monkeypatch.setattr(hook, "resub", fake_resub)
    return got


def near_fix(fix, offset):
    return fix["rate"] == "1/1" and abs(fix["offset"] - offset) <= 0.01


def test_sub_time_times_the_tracks_the_word_check_cannot_read(env, monkeypatch):
    """A French track and a Spanish PGS track run 2 s late, and a German track holds another episode. The English track
    matched in time, so it is the reference. The French and the PGS track get their fix in one remux. The German track
    is a weak fit: it only reports, with no fix and no alert. A forced and a commentary track stay out."""
    subs = [("eng", False, {}), ("fre", False, {}), ("spa", False, {"codec_id": "S_HDMV/PGS"}), ("ger", False, {}),
            ("eng", False, {"forced_track": True, "track_name": "Forced"}), ("fre", False, {"track_name": "Commentary"})]
    late = talk.moved_to(REF, offset=2.0)
    got = sub_time_film(env, monkeypatch, subs, {"s1": REF, "s2": late, "s4": talk.show(12, n=170), "s5": late, "s6": late},
                        pictures={"s3": [(a, b, "") for a, b, _ in late]})
    hook.main(["--sub-time", env["path"]])
    rec = decided(env)
    assert set(rec["subtime"]) == {"s2", "s3", "s4"} and rec["subtime"]["s4"]["verdict"] == "weak", rec["subtime"]
    (apply, fixes, drop), = got
    assert not apply and sorted(fixes) == [3, 4] and all(near_fix(f, 2.0) for f in fixes.values()) and drop == [], got
    assert rec["alert_kinds"] == ["subtiming"] and [(x["code"], x["tracks"], x["block"]) for x in rec["findings"][0]["lines"]] == [
        ("not_retimed", ["s2", "s3"], {"code": "remux"})], rec["findings"]
    assert not [u for m, u, b in env["http"] if "discord" in u]   # a weak fit alerts nothing, and nothing posts
    assert {p: (r["lang"], r["role"], r["reference"], r["verdict"], round(r["score"], 2), r["timing"]["fix"]["rate"], round(r["timing"]["fix"]["offset"], 2))
            for p, r in rec["subtime"].items() if p != "s4"} == {"s2": ("fre", "full", "s1", "fit", 1.0, "1/1", 2.0),
                                                                 "s3": ("spa", "full", "s1", "fit", 1.0, "1/1", 2.0)}, rec["subtime"]
    # the remux would retime s2 and s3. s4 only reports.
    assert (rec["subremux"]["fixed"], rec["subremux"]["remove"], rec["subremux"]["codes"], rec["subremux"]["done"]) == (
        ["s2", "s3"], [], ["would_remux_subtitles"], False), rec["subremux"]
    s1 = rec["subcheck"]["s1"]   # the word check's track, the reference
    assert (rec["references"], s1["verdict"], s1["timing"]) == ({"s1": "in time"}, "match", {"fix": None, "why": "in time"}), rec


def test_sub_time_applies_and_analyzes_in_plex(env, monkeypatch):
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    hook.main(["--sub-time", env["path"], "--apply"])
    rec = decided(env)
    assert got[0][0] and list(got[0][1]) == [3] and rec["subremux"]["codes"] == ["subtitle_retimed"], got
    rm = rec["subremux"]   # no kept_how: the kept original is a hard link
    assert (rm["done"], rm["kept"], rm.get("kept_how")) == (True, "/kept/f.mkv", None), rm
    assert analyzes(env) == ["/library/metadata/7101/analyze"] and ("POST", "command", {"name": "RescanMovie", "movieId": 7}) in env["writes"], env["writes"]


def test_sub_time_carries_the_reference_fix_into_the_target(env, monkeypatch):
    """The English reference runs 2 s late, and the word check fixes it. The French track runs 2 s late too. Against
    the fixed reference it is 2 s late, so it gets the same fix, in the same remux."""
    late = talk.moved_to(REF, offset=2.0)
    fixed = dict(IN_TIME, timing={"fix": {"rate": "1/1", "offset": 2.0}, "why": "a fix of +2.00 s"})
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": late, "s2": late}, sync={"s1": fixed})
    rows = [{"at": 60.0 * k, "words": 15, "overlap": 0.9, "cues": 4, "offset": 2.0, "off": 0.0} for k in range(1, 12)]   # the sweep confirms the fix
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({"s1": rows}, {"cpu": 0, "took": 0, "failed": []}))
    hook.main(["--sub-time", env["path"]])
    (apply, fixes, drop), = got
    assert sorted(fixes) == [2, 3] and near_fix(fixes[3], 2.0), fixes


def test_sub_time_with_no_reference_says_why(env, monkeypatch, capsys):
    unmatched = dict(IN_TIME, verdict="unknown", why="2 of 2 windows hold under 8 heard words", timing=None)
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {}), ("spa", False, {"codec_id": "S_HDMV/PGS"})],
                        {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)}, sync={"s1": unmatched})
    monkeypatch.setattr(hook, "picture_cues", lambda *a, **k: pytest.fail("with no reference no track is read"))
    hook.main(["--sub-time", env["path"]])
    r = decided(env)["subtime"]["s2"]
    assert got == [] and r["verdict"] == "unknown" and r["why"].startswith("no track or sidecar of the file matched the audio"), r
    assert "| reference, none | unknown |" in capsys.readouterr().out


def test_a_word_check_removal_and_a_reference_retime_share_one_remux(env, monkeypatch, settings):
    settings(keep_days=7)
    wrong = dict(IN_TIME, verdict="mismatch", why="the heard words match the cues at 3%, 5% at best", timing=None)
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("eng", False, {"track_name": "SDH"}), ("fre", False, {})],
                        {"s1": REF, "s2": REF, "s3": talk.moved_to(REF, offset=2.0)}, sync={"s1": IN_TIME, "s2": wrong})
    hook.main(["--sub-time", env["path"], "--apply"])
    (apply, fixes, drop), = got
    assert apply and list(fixes) == [4] and drop == [3] and decided(env)["subremux"]["codes"] == ["subtitle_retimed", "subtitle_mismatch_removed"], got


def test_sub_time_retimes_an_other_language_sidecar_and_keeps_its_original(env, monkeypatch, settings, tmp_path, capsys):
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    got = sub_time_film(env, monkeypatch, [("eng", False, {})], {"s1": REF}, sides={".es.srt": talk.moved_to(REF, offset=2.0)})
    hook.main(["--sub-time", env["path"], "--apply"])
    rec = decided(env)
    e, = rec["sidecars"]
    assert got == [] and e["action"] == "retime" and e["result"] == "retimed" and e["kept"].startswith(str(tmp_path / ".kept")), rec["sidecars"]
    first = hook.sidecar_subs(env["path"])[0]["cues"][0]
    assert abs(first[0] / 1000 - REF[0][0]) <= 0.002, first


def test_a_cut_found_by_the_reference_alerts_in_print_and_never_posts(env, monkeypatch, capsys):
    cut = talk.moved_to(REF, offset=2.0, cut=(talk.DURATION / 2, 3.0))
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": cut})
    hook.main(["--sub-time", env["path"]])
    rec, out = decided(env), capsys.readouterr().out
    (line,) = rec["findings"][0]["lines"]
    assert got == [] and rec["alert_kinds"] == ["subtiming"] and (line["code"], line["track"], line["ref"]) == ("off", "s2", "s1"), rec["findings"]
    assert line["why"].startswith("the cues are off by"), line
    assert "  ALERT subtiming: " in out and not [u for m, u, b in env["http"] if "discord" in u], out


@pytest.mark.parametrize("cause", ["no detection", "short file"])
def test_sub_time_says_why_it_checked_no_subtitle(env, monkeypatch, capsys, cause):
    """With no language detection, or a file under SUB_MIN_SECONDS, the check hears nothing, and --sub-time says why."""
    sub_time_film(env, monkeypatch, [("eng", False, {})], {"s1": REF})
    if cause == "no detection":
        monkeypatch.setattr(hook, "lid_ready", lambda: False)
    else:
        monkeypatch.setattr(hook, "SUB_MIN_SECONDS", 10 ** 6)
    hook.main(["--sub-time", env["path"]])
    out = capsys.readouterr().out
    assert not decided(env).get("subcheck")
    assert {"no detection": "because language detection is not installed", "short file": "because it runs under 16666 minutes"}[cause] in out, out
    sub_time_film(env, monkeypatch, [("eng", False, {})], {"s1": REF})
    monkeypatch.setattr(hook, "SUB_MIN_SECONDS", 300)
    hook.main(["--sub-time", env["path"]])
    assert "skipped the file" not in capsys.readouterr().out   # a file the check heard gets no such line
    assert hook.unheard({"outcome": "error"}) is None   # a run that failed before the probe names no length


def test_sub_time_ignores_the_cache_and_writes_it(env, monkeypatch, capsys):
    """A --sub-check backfill skips a file whose verdicts are cached. --sub-time checks it anyway and writes the cache."""
    sub_time_film(env, monkeypatch, [("eng", False, {})], {"s1": REF})
    hook.main(["--sub-time", env["path"]])
    assert hook.sub_cached(env["path"])
    hook.main(["--sub-time", env["path"]])
    assert len([r for r in log_lines(env) if r.get("outcome")]) == 2


def test_sub_time_finds_the_item_with_one_list_call(env, monkeypatch, tmp_path, capsys):
    """Radarr lists its movies once. Sonarr lists its series once, and reads the episode files of the series whose folder
    holds the path, never of another series. The folder of Show A is a prefix of the folder of Show AB and holds none
    of its files. A path no app lists runs with no item."""
    show = tmp_path / "tv" / "Show AB"
    show.mkdir(parents=True)
    ep = show / "Show AB - S01E02.mkv"
    shutil.copy(env["path"], ep)
    other = tmp_path / "loose.mkv"
    shutil.copy(env["path"], other)
    calls, seen = [], []

    def fake_arr(app, p):
        calls.append((app, p.split("?")[0] if "seriesId" not in p else p))
        return {("radarr", "movie"): [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})], ("radarr", "qualityprofile"): [],
                ("sonarr", "series"): [{"id": 4, "title": "Show A", "path": str(tmp_path / "tv" / "Show A")}, {"id": 5, "title": "Show AB", "path": str(show)},
                                       {"id": 6, "title": "Show B", "path": str(tmp_path / "tv" / "Show B")}],
                ("sonarr", "episode?seriesId=5"): [{"episodeFileId": 21, "seasonNumber": 1, "episodeNumber": 2, "runtime": 20}],
                ("sonarr", "episodefile?seriesId=5"): [{"id": 21, "path": str(ep), "seriesId": 5}]}[(app, p)]
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook, "PROFILES", {})
    monkeypatch.setattr(hook, "process", lambda c: seen.append((c.app, c.path, c.label, c.original, c.runtime, c.mode == "sub_time"))
                        or {"outcome": "no_change", "result": "no change", "path": c.path, "label": c.label})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--sub-time", env["path"], str(ep), str(other)])
    assert calls == [("radarr", "movie"), ("radarr", "qualityprofile"), ("sonarr", "series"), ("sonarr", "episode?seriesId=5"),
                     ("sonarr", "episodefile?seriesId=5")], calls
    assert seen == [("radarr", env["path"], "Film A (1979)", "English", 120, True), ("sonarr", str(ep), "Show AB S01E02", None, 20, True),
                    (None, str(other), "loose.mkv", None, 0, True)], seen
    assert f"no app lists it, so it knows no original language: {other}" in capsys.readouterr().out


def test_sub_time_on_a_missing_file_says_so_and_exits_4(env, monkeypatch, capsys):
    unlisted(env, monkeypatch)
    gone = os.path.join(os.path.dirname(env["path"]), "Gone.mkv")
    with pytest.raises(SystemExit) as ex:
        hook.main(["--sub-time", gone])
    assert ex.value.code == hook.SUB_TIME_NO_FILE == 4 and f"no file at the path, skipped: {gone}" in capsys.readouterr().out


def test_a_job_whose_episode_ids_repeat_asks_the_app_for_each_id_once(monkeypatch, tmp_path):
    """Sonarr answers 500 to a list that names an id twice. The job's own episode is in its unit from the start, the app
    lists it again, and a job may carry it twice. Each list the hook builds names it once."""
    asked = []

    def fake_arr(app, p):
        asked.append(p)
        if p.startswith("episode?episodeIds="):
            assert p == "episode?episodeIds=27", p
            return [{"id": 27, "episodeFileId": 70, "seriesId": 5, "runtime": 22}]
        if p == "episode?episodeFileId=70":
            return [{"id": 27}]
        if p == "episodefile/70":
            return {"seriesId": 5, "path": str(tmp_path / "S01E02.mkv")}
        if p == "rootfolder":
            return [{"path": str(tmp_path)}]
        raise AssertionError(p)
    monkeypatch.setattr(hook, "arr", fake_arr)
    job = {"owner": "5", "file_id": "70", "episode_ids": "27,27", "path": "/tv/gone/S01E02.mkv"}
    assert hook.unit_files("sonarr", [], job)[70]["items"] == [27] and hook.job_episodes({"episode_ids": "28,27,28"}) == [27, 28]
    (tmp_path / "S01E02.mkv").write_bytes(b"x")
    assert hook.moved("sonarr", job) == (str(tmp_path / "S01E02.mkv"), None)   # the app's one episode is the job's


@pytest.mark.parametrize("fail", [TimeoutError("timed out"), ConnectionResetError("the app restarts")])
def test_sub_time_never_applies_when_a_lookup_fails(env, monkeypatch, capsys, fail):
    """A lookup that fails gives no original language, and a decision without it can turn the wrong tracks on. So
    --apply stops before any change. A dry run goes on and says why it knows no original language."""
    def fake_arr(app, p):
        if app == "radarr":
            raise fail
        return []
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    seen = []
    monkeypatch.setattr(hook, "process", lambda c: seen.append(c.path) or {"outcome": "no_change", "result": "no change", "path": c.path, "label": "x"})
    with pytest.raises(SystemExit) as ex:
        hook.main(["--sub-time", "--apply", env["path"]])
    assert seen == [] and "--sub-time --apply stops, and nothing changed: the radarr lookup failed" in str(ex.value), ex.value
    hook.main(["--sub-time", env["path"]])
    assert seen == [env["path"]] and f"no app could be asked, so this dry run knows no original language: {env['path']}" in capsys.readouterr().out


REAL_ARR, REAL_RESUB = hook.arr, hook.resub   # the env fixture fakes the app API, and the subtitle tests fake the remux


def env_values(*names):
    """The KEY='value' lines of the env files names, the last one winning, as the image's first start writes them."""
    out = {}
    for n in names:
        for line in open(os.path.join(FILES, n)):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                out[k] = v.strip("'\"")
    return out


def nothing_set(monkeypatch, tmp_path, urls=None):
    """No app is set up: no API key and no config.xml. urls holds the <APP>_URL values, empty by default."""
    apps = {app: dataclasses.replace(a, dir=str(tmp_path / "no-app"), api_key="", url=(urls or {}).get(f"{app.upper()}_URL", ""))
            for app, a in hook.CFG.apps.items()}
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, apps=apps))
    monkeypatch.setattr(hook, "arr", REAL_ARR)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)


def not_writable(monkeypatch, folder):
    """keepable() finds folder not writable, as for a uid that does not own it. A mode would not stop root."""
    real = os.access
    monkeypatch.setattr(hook.os, "access", lambda p, mode, **k: False if str(p) == str(folder) and mode == os.W_OK else real(p, mode, **k))


def test_the_env_files_ship_only_the_urls_that_set_up_no_app():
    urls = {k: v for n in ("examples/arr-media-guard.env", "docker/arr-media-guard.env") for k, v in env_values(n).items() if k in ("RADARR_URL", "SONARR_URL")}
    assert urls and all(v in hook.SHIPPED_URLS[k[:-4].lower()] for k, v in urls.items()), urls


@pytest.mark.parametrize("setup", ["none", "image", "host one app", "url of its own"])
def test_sub_time_says_nothing_of_an_app_the_user_did_not_set_up(env, monkeypatch, settings, capsys, tmp_path, setup):
    """An app with no API key and no config.xml, and no URL or a URL as the env files ship it, is not set up. It stays
    silent, and with no app set up one line says so for every path. The image writes both env files, so a one-off
    container has both URLs. A host that runs Radarr only asks Radarr. A URL of the user's own with no key is a setup in
    part: it fails as a lookup does, and --apply stops."""
    other = tmp_path / "loose.mkv"
    shutil.copy(env["path"], other)
    urls = {"none": {}, "image": env_values("examples/arr-media-guard.env", "docker/arr-media-guard.env"),
            "host one app": env_values("examples/arr-media-guard.env"), "url of its own": {"RADARR_URL": "http://media-box.invalid:7878"}}[setup]
    nothing_set(monkeypatch, tmp_path, {k: v for k, v in urls.items() if k in ("RADARR_URL", "SONARR_URL")})
    if setup == "host one app":
        (tmp_path / "radarr").mkdir()
        (tmp_path / "radarr" / "config.xml").write_text("<Config><ApiKey>k3y</ApiKey></Config>")
        settings(radarr={"dir": str(tmp_path / "radarr")})   # Radarr lists no movie, see the env fixture's Plex-only http
    seen = []
    monkeypatch.setattr(hook, "process", lambda c: seen.append((c.app, c.path)) or {"outcome": "no_change", "result": "no change", "path": c.path, "label": "x"})
    hook.main(["--sub-time", env["path"], str(other)])
    out = capsys.readouterr().out.splitlines()
    assert seen == [(None, env["path"]), (None, str(other))] and not [x for x in out if "sonarr" in x.lower() and setup != "none" and "set up, so" not in x], out
    if setup in ("none", "image"):
        assert out[0] == ("no Sonarr or Radarr is set up, so this run knows no original language. Set SONARR_URL and SONARR_API_KEY, or "
                          "RADARR_URL and RADARR_API_KEY, so an app can name the original language.") \
            and not [x for x in out if "knows no original language:" in x or "API key" in x], out
    elif setup == "host one app":
        assert out[:2] == [f"no app lists it, so it knows no original language: {p}" for p in (env["path"], other)], out
    else:
        failed = (f"RADARR_URL is set, but the Radarr API key does not read: [Errno 2] No such file or directory: "
                  f"'{tmp_path / 'no-app' / 'config.xml'}'. Set RADARR_API_KEY.")
        assert out[:3] == [failed] + [f"no app could be asked, so this dry run knows no original language: {p}" for p in (env["path"], other)], out
        with pytest.raises(SystemExit) as ex:
            hook.main(["--sub-time", env["path"], "--apply"])
        assert str(ex.value) == f"--sub-time --apply stops, and nothing changed: {failed}" and len(seen) == 2, ex.value


@pytest.mark.parametrize("argv", [["--backfill", "radarr"], ["--backfill", "sonarr", "--sub-check"], ["--backfill", "radarr", "--check-video"]])
def test_a_backfill_of_an_app_that_is_not_set_stops_with_one_line(env, monkeypatch, tmp_path, argv):
    nothing_set(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as ex:
        hook.main(argv)
    assert str(ex.value) == f"{argv[1].capitalize()} is not set up. Set {argv[1].upper()}_URL and {argv[1].upper()}_API_KEY.", ex.value


def test_the_audit_of_an_app_that_is_not_set_says_it_prunes_nothing(env, monkeypatch, settings, capsys, tmp_path):
    nothing_set(monkeypatch, tmp_path)
    settings(keep_days=7)
    hook.main(["--audit", "radarr", "--since", "24h"])
    out = capsys.readouterr().out
    assert out.startswith("Radarr is not set up, so the audit prunes no kept folder under its root folders.\n") and "not pruned" not in out, out


@pytest.mark.parametrize("case", ["writable", "no keep folder", "keep folder", "size cap", "low space", "no room"])
def test_a_dry_run_says_what_apply_would_do_with_the_remux(env, monkeypatch, settings, capsys, tmp_path, case):
    """A one-shot dry run with no app set on a file whose cues flash. --apply would remux the file, or skip the remux
    for a reason the dry run names with what to fix. Without the keep folder, creating it is enough. The dry run prints
    the planned tense, because it changes no file. A --sub-check backfill prints the same. --apply prints the done
    tense."""
    flash_film(env, monkeypatch)
    monkeypatch.setattr(hook, "resub", REAL_RESUB)
    settings(keep_days=7)
    keep = tmp_path / ".kept"
    monkeypatch.setattr(hook, "originals_root", lambda p: str(keep))
    monkeypatch.setattr(hook.os, "getuid", lambda: 80)   # PUID and PGID, as the tester ran it
    monkeypatch.setattr(hook.os, "getgid", lambda: 81)
    if case == "keep folder":
        keep.mkdir()
    if case in ("no keep folder", "keep folder"):
        not_writable(monkeypatch, keep if case == "keep folder" else tmp_path)
    if case == "size cap":
        settings(repack_max=999)
    if case == "low space":
        monkeypatch.setattr(hook.os, "statvfs", lambda p: types.SimpleNamespace(f_bavail=1, f_frsize=1000))
    if case == "no room":   # a keep folder on another file system, with no room for a copy
        monkeypatch.setattr(hook, "keepable", lambda p, st: f"{tmp_path} is on another file system with 0.1 GB free, too little for a copy of 0.0 GB")
    fake_arr = hook.arr
    nothing_set(monkeypatch, tmp_path)
    hook.main(["--sub-time", env["path"]])
    out, rec = capsys.readouterr().out, decided(env)
    user = "uid 80 and gid 81"
    block = {"writable": {"code": "remux"}, "no keep folder": {"code": "keep_create", "root": str(keep), "user": user},
             "keep folder": {"code": "keep_root", "root": str(keep), "user": user}, "size cap": {"code": "cap", "why": "over the 0 GB repack cap"},
             "low space": {"code": "space", "folder": os.path.dirname(env["path"]), "free": 1000 / 1e9, "need": 2 * os.path.getsize(env["path"]) / 1e9},
             "no room": {"code": "keep", "why": f"{tmp_path} is on another file system with 0.1 GB free, too little for a copy of 0.0 GB"}}[case]
    result = {"writable": "would remux subtitles", "no keep folder": f"subtitle remux skipped, the original cannot be kept: {tmp_path} is not writable",
              "keep folder": f"subtitle remux skipped, the original cannot be kept: {keep} is not writable",
              "size cap": "subtitle remux skipped, over the 0 GB repack cap", "low space": "subtitle remux skipped, low space: 0.0 GB free for 0.0 GB",
              "no room": f"subtitle remux skipped, the original cannot be kept: {tmp_path} is on another file system with 0.1 GB free, too "
                         "little for a copy of 0.0 GB"}[case]
    line = {"code": "not_retimed", "tracks": ["s1"], "result": f"{result}: track 2: new ends for 40 of 40 cues", "block": block}
    codes = ["would_remux_subtitles" if case == "writable" else "subtitle_remux_skipped"]
    assert not rec["apply"] and rec["findings"] == [{"kind": "subtiming", "lines": [line]}], rec["findings"]
    assert (rec["flash"]["s1"]["lengthened"], rec["flash"]["s1"]["cues"], rec["subremux"]["ended"], rec["subremux"]["codes"]) == (40, 40, ["s1"], codes), rec
    assert hook.sub_time_report(rec, "planned") in out, out
    monkeypatch.setattr(hook, "arr", fake_arr)
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    hook.main(["--backfill", "radarr", "--sub-check"])
    out, back = capsys.readouterr().out, [r for r in log_lines(env) if r.get("outcome")][-1]
    assert (back["apply"], back["findings"], back["subremux"]["codes"]) == (False, rec["findings"], codes), back
    assert hook.render(back, "cli", "planned") in out, out
    if case != "writable":
        nothing_set(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as ex:
            hook.main(["--sub-time", env["path"], "--apply"])
        done = [r for r in log_lines(env) if r.get("outcome")][-1]
        assert ex.value.code == hook.SUB_TIME_MISSED and done["apply"] and done["findings"] == [{"kind": "subtiming", "lines": [dict(line, block=None)]}], done
        assert hook.sub_time_report(done, "done") in capsys.readouterr().out


@pytest.mark.parametrize("case", ["hardlinked", "no keep folder", "KEEP_ORIGINALS_DAYS 0"])
def test_a_dry_run_says_what_apply_would_do_with_a_track_that_does_not_match(env, monkeypatch, settings, capsys, tmp_path, case):
    """The English subtitle, flagged default, holds another film's lines. The remux would remove it, through the real
    resub(). A hardlinked file stays as it is under --apply, flags included. Without a writable keep folder the track
    would stay and lose its flags. With KEEP_ORIGINALS_DAYS 0 no removal is planned, and --apply turns the flags off.
    --apply prints the done tense."""
    why = "the heard words match the cues at 10%, 12%"
    sub_time_film(env, monkeypatch, [("eng", True, {})], {"s1": REF}, sync={"s1": dict(IN_TIME, verdict="mismatch", why=why)})
    monkeypatch.setattr(hook, "resub", REAL_RESUB)
    keep = tmp_path / ".kept"
    monkeypatch.setattr(hook, "originals_root", lambda p: str(keep))
    settings(keep_days=0 if case == "KEEP_ORIGINALS_DAYS 0" else 7)
    monkeypatch.setattr(hook.os, "getuid", lambda: 80)
    monkeypatch.setattr(hook.os, "getgid", lambda: 81)
    if case == "hardlinked":
        os.link(env["path"], tmp_path / "seed.mkv")   # the download client's copy
    if case == "no keep folder":
        not_writable(monkeypatch, tmp_path)
    nothing_set(monkeypatch, tmp_path)
    hook.main(["--sub-time", env["path"]])
    out, rec = capsys.readouterr().out, decided(env)
    keep_days = case == "KEEP_ORIGINALS_DAYS 0"
    result = {"hardlinked": "subtitle remux skipped, hardlinked: remove track 2",
              "no keep folder": f"subtitle remux skipped, the original cannot be kept: {tmp_path} is not writable: remove track 2",
              "KEEP_ORIGINALS_DAYS 0": None}[case]
    line = {"code": "stays", "track": "s1", "why": why, "gone": not keep_days, "result": result, "kept_back": "keep_days" if keep_days else None,
            "flags_off": True}
    plan = {"hardlinked": {"hardlinked": True, "block": {"code": "hardlinked", "why": "hardlinked"}},
            "no keep folder": {"hardlinked": False, "block": {"code": "keep_create", "root": str(keep), "user": "uid 80 and gid 81"}},
            "KEEP_ORIGINALS_DAYS 0": {"hardlinked": False, "block": None}}[case]
    assert not rec["apply"] and rec["findings"] == [{"kind": "submatch", "lines": [dict(line, **plan)]}], rec["findings"]
    assert hook.sub_time_report(rec, "planned") in out, out
    assert rec["outcome"] == ("hardlinked" if case == "hardlinked" else "dry_run"), rec["outcome"]
    nothing_set(monkeypatch, tmp_path)
    if case == "KEEP_ORIGINALS_DAYS 0":   # the flag edit is the whole change, and it happens
        hook.main(["--sub-time", env["path"], "--apply"])
    else:
        with pytest.raises(SystemExit) as ex:
            hook.main(["--sub-time", env["path"], "--apply"])
        assert ex.value.code == hook.SUB_TIME_MISSED
    done = [r for r in log_lines(env) if r.get("outcome")][-1]
    assert done["apply"] and done["findings"] == [{"kind": "submatch", "lines": [line]}], done["findings"]
    assert hook.sub_time_report(done, "done") in capsys.readouterr().out


def test_an_import_keeps_its_alert_when_the_original_cannot_be_kept(env, monkeypatch, settings, tmp_path):
    """The import path finds the same as a dry run. An import is an apply, so it logs and posts the done tense."""
    flash_film(env, monkeypatch)
    monkeypatch.setattr(hook, "resub", REAL_RESUB)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    not_writable(monkeypatch, tmp_path)
    hook.main([])
    rec = decided(env)
    assert rec["apply"] and rec["findings"] == [{"kind": "subtiming", "lines": [{
        "code": "not_retimed", "tracks": ["s1"], "block": None,
        "result": f"subtitle remux skipped, the original cannot be kept: {tmp_path} is not writable: track 2: new ends for 40 of 40 cues"}]}]
    (b,) = [b for m, u, b in env["http"] if "discord" in u]
    assert b["embeds"] == hook.render(rec, "embed", "done"), b["embeds"]


def test_a_sweep_part_off_the_fitted_line_goes_to_the_log_only(env, monkeypatch):
    """The sweep hears one window a minute. The cues of the middle minutes sit 1.5 s late, where no window of the
    check lies, so the check finds the track in time. The sweep's rows there sit 1.5 s off the fitted line. They are
    a small part of the file, so the rows stay in the decision line and no alert goes out."""
    english_film(env, ("eng", False, {}))
    mid = lambda i: 1.5 if 0.45 * talk.DURATION <= talk.FIRST + talk.GAP * i < 0.55 * talk.DURATION else 0.0
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, where=mid)})
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("the sweep only reports"))
    rec = hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", round(talk.DURATION / 60), mode="sub_time", apply=False,
                                post=False))
    rows = rec["sweep"]["s1"]
    assert rec["subcheck"]["s1"]["timing"]["why"] == "in time" and len(rows) == len({round(w["at"] // 60) for w in rows}) >= 15, rows
    far = [w for w in rows if hook.sweep_far(w)]
    assert far and all(0.45 * talk.DURATION - 10 <= w["at"] <= 0.55 * talk.DURATION and abs(w["off"] - 1.5) <= 0.1 for w in far), far
    assert [len(ws) for _, _, ws in env["words"][1:]] == [len(rows)] and env["group"] == [2], env["words"]   # one process hears the sweep
    assert hook.sweep_steps(rows) and not hook.sweep_alerts(rows) and rec["findings"] == [] and rec["alert_kinds"] == [], rec["findings"]


def test_sweep_far_needs_enough_cues_and_a_step():
    row = {"at": 60.0, "words": 20, "overlap": 0.9, "cues": 3, "offset": 1.0, "off": 1.0}
    assert hook.sweep_far(row) and not hook.sweep_far(dict(row, cues=2)) and not hook.sweep_far(dict(row, off=0.99))
    assert hook.sweep_far(dict(row, off=-1.0)) and not hook.sweep_far(dict(row, off=None))


def test_sweep_steps_need_two_neighbouring_windows_off_the_same_way():
    row = lambda at, off, cues=4: {"at": at, "words": 20, "overlap": 0.9, "cues": cues, "offset": off, "off": off}
    one = [row(60, 0.1), row(120, 1.07), row(180, 0.12)]
    two = [row(60, 0.1), row(120, 1.2), row(180, None, 0), row(240, 1.3), row(300, 0.1)]   # an untrusted row between them
    assert hook.sweep_steps(one) == set() and hook.sweep_steps(two) == {id(two[1]), id(two[3])}
    assert hook.sweep_steps([row(60, 1.2), row(120, -1.3)]) == set() and hook.sweep_steps([row(60, 1.2), row(120, 1.1, 2)]) == set()


# The sweep rows of two real files, (at, cues, off): an episode whose subtitles sit 139 s late through the file, and one
# whose subtitles sit 1.7 to 2.5 s late at two windows near its end, in time everywhere else
SWEEP_LATE = [(142.3, 2, 139.85), (221.4, 2, 138.66), (257.8, 3, 139.06), (346.4, 1, 139.17), (397.5, 2, 139.02), (444.2, 2, 138.69),
              (499.9, 3, 138.87), (585.5, 2, 138.5), (602.9, 1, 138.94), (672.2, 1, 138.61), (725.2, 4, 138.43), (784.4, 0, None),
              (864.2, 2, 138.49), (900.0, 3, 138.68), (998.9, 1, 138.66), (1040.5, 2, 138.6), (1124.5, 0, None), (1140.0, 0, None),
              (1211.5, 0, None), (1341.5, 0, None), (1413.3, 0, None), (1442.3, 0, None)]
SWEEP_END = [(44.4, 3, 1.45), (61.2, 1, 0.54), (120.0, 2, 0.77), (195.0, 2, 1.92), (240.0, 0, None), (308.1, 0, None), (408.2, 4, -0.01),
             (456.3, 3, 0.74), (491.2, 2, -0.25), (540.0, 3, 0.29), (623.2, 3, 0.37), (660.0, 3, 0.55), (737.1, 2, 0.36), (810.5, 2, -0.26),
             (840.3, 3, 1.36), (904.1, 1, 1.18), (960.0, 3, 0.75), (1043.9, 3, 0.76), (1128.9, 2, -0.1), (1144.4, 1, 0.43), (1207.0, 3, 0.64),
             (1272.6, 3, 0.85), (1366.4, 3, 2.54), (1381.6, 3, 1.66), (1444.5, 3, 0.15), (1530.0, 2, 1.25), (1591.9, 1, 0.69),
             (1653.6, 1, 0.46), (1723.7, 3, 0.69), (1786.1, 2, 0.81), (1813.5, 2, 0.9), (1861.8, 1, 1.55), (1926.6, 2, 0.64),
             (2015.1, 2, -0.91), (2041.1, 3, -0.58), (2122.2, 2, 1.53), (2160.0, 1, 0.21), (2248.8, 0, None), (2311.2, 4, -0.43),
             (2381.7, 3, 0.02), (2448.2, 3, -0.03), (2475.1, 3, 0.73), (2552.6, 2, -0.18), (2580.0, 4, 0.02), (2676.0, 2, -0.07),
             (2740.2, 3, -0.46), (2791.8, 4, 0.2), (2865.3, 4, -0.24), (2880.0, 4, -1.01), (2962.6, 3, 0.73), (3023.7, 4, 0.88),
             (3060.0, 3, 0.44), (3136.4, 3, 0.55), (3204.1, 2, 0.51), (3254.4, 3, 0.16), (3325.3, 2, 1.89), (3367.9, 3, 0.29),
             (3461.9, 3, 1.96), (3503.4, 3, 0.99), (3540.0, 2, 1.19), (3647.9, 2, -0.05), (3695.5, 3, -0.78), (3769.1, 1, 0.54),
             (3825.9, 4, -0.14), (3840.0, 0, None)]


def test_the_sweep_alerts_when_three_step_rows_are_a_quarter_of_the_file(capsys):
    """A step alerts when it holds 3 rows or more, or every row to trust, and at least a quarter of the rows to trust:
    the late track, 4 of 4 rows, the last half of a track, its middle 8 of 20 rows, and a sparse sweep whose 2 rows to
    trust are both off. Two windows near the end go to the decision line only, because nothing failed and nothing can
    be done. So do two stray windows of a sparse sweep, a step of 2 of 4 rows, and a step of 3 of 20 rows. The report
    gives the share of a step that does not alert. Every file keeps every row in the log."""
    rows = lambda xs: [{"at": at, "words": 15, "overlap": 0.9, "cues": cues, "offset": off, "off": off} for at, cues, off in xs]
    late, end = rows(SWEEP_LATE), {"s1": rows(SWEEP_END), "s2": rows(SWEEP_END)}
    (f,) = hook.sub_findings({"sweep": {"s1": late}}, {}, set())
    assert f == {"kind": "subtiming", "lines": [{"code": "sweep", "far": [["s1", 257.8, 139.06], ["s1", 499.9, 138.87], ["s1", 725.2, 138.43],
                                                                         ["s1", 900.0, 138.68]]}]}
    assert hook.sweep_steps(end["s1"]) == {id(end["s1"][22]), id(end["s1"][23])} and hook.sub_findings({"sweep": end}, {}, set()) == []
    tail = rows([(60.0 * k, 3, 2.5 if k > 10 else 0.1) for k in range(1, 21)])
    middle = rows([(60.0 * k, 3, 3.0 if 7 <= k < 15 else 0.1) for k in range(1, 21)])
    both = rows([(60, 3, 5.0), (120, 1, 0.2), (180, 4, 5.1)])   # 2 rows to trust, both 5 s off
    assert [len(hook.sweep_alerts(x)) for x in (late, tail, middle, both)] == [4, 10, 8, 2]
    sparse = rows([(60, 3, 0.2), (120, 2, 0.4), (180, 1, 0.3), (240, 2, 0.1), (300, 3, 1.7), (360, 3, 2.5)])
    half = rows([(60, 3, 0.1), (120, 3, 1.5), (180, 3, 1.4), (240, 3, 0.2)])
    short = rows([(60.0 * k, 3, 2.0 if k > 17 else 0.1) for k in range(1, 21)])   # 3 of 20 rows, under a quarter
    assert [len(hook.sweep_steps(x)) for x in (sparse, half, short)] == [2, 2, 3] and not any(map(hook.sweep_alerts, (sparse, half, short)))
    report = hook.sub_time_report({"result": "no change", "label": "Show S28E41", "path": "/m/x.mkv", "sweep": dict(end, s3=half)}, "done")
    assert "+2.54 s off the fitted line, in a step of 2 of the 35 windows that heard 3 cues" in report and "ALERT" not in report, report
    assert "+1.50 s off the fitted line, in a step of 2 of the 4 windows that heard 3 cues" in report, report


def test_one_sweep_window_off_the_line_goes_to_the_log_only(env, monkeypatch, capsys):
    """Whisper heard a word with the line before it, so one window of the sweep sits 1.07 s off. Its neighbours sit on
    the line. No alert goes out, and the report marks the window."""
    sub_time_film(env, monkeypatch, [("eng", False, {})], {"s1": REF})
    rows = [{"at": 60.0 * k, "words": 15, "overlap": 0.9, "cues": 4, "offset": 1.07 if k == 4 else 0.1, "off": 1.07 if k == 4 else 0.1}
            for k in range(1, 12)]
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({"s1": rows}, {"cpu": 0, "took": 0, "failed": []}))
    hook.main(["--sub-time", env["path"]])
    rec, out = decided(env), capsys.readouterr().out
    assert rec["findings"] == [] and "+1.07 s off the fitted line, one window alone" in out, (rec["findings"], out)


def test_resub_keeps_a_cover_attachment_out_of_the_streams(sync_mkv, tmp_path, monkeypatch, settings):
    """ffmpeg reads an image attachment as a cover picture stream and would write it back as a video track. The remux
    leaves it out of the map and mkvpropedit adds it back with its name, type and UID, so the tracks stay the same."""
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:size=32x32", "-frames:v", "1", str(tmp_path / "cover.png")], check=True)
    path = str(tmp_path / "f.mkv")
    REAL_RUN(["mkvmerge", "-q", "-o", path, str(sync_mkv / "f.mkv"), "--attachment-mime-type", "image/png", "--attach-file", str(tmp_path / "cover.png")],
             check=True)
    settings(keep_days=0)
    j = REAL_MKVMERGE(path)
    _, result, info = hook.resub(path, j, os.stat(path), True, {2: {"rate": "1/1", "offset": 1.0}})
    new = REAL_MKVMERGE(path)
    assert result == "subtitles remuxed" and [t["codec"] for t in new["tracks"]] == [t["codec"] for t in j["tracks"]], (result, new["tracks"])
    assert new["attachments"] == j["attachments"]


def test_resub_writes_nothing_to_the_system_temp_dir(pic_mkv, tmp_path, monkeypatch, settings):
    """A remux with a cover, a track that gets a ratio fix and new ends, and a track that gets new ends. The system temp
    dir does not exist, so any file there would fail the remux. The work folder under STATE_DIR goes after it."""
    REAL_RUN(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:size=32x32", "-frames:v", "1", str(tmp_path / "cover.png")], check=True)
    path = str(tmp_path / "flash.mkv")
    REAL_RUN(["mkvmerge", "-q", "-o", path, str(pic_mkv / "flash.mkv"), "--attachment-mime-type", "image/png", "--attach-file", str(tmp_path / "cover.png")],
             check=True)
    settings(keep_days=0)
    j = REAL_MKVMERGE(path)
    plans = hook.flash_check(path, j, [])
    monkeypatch.setattr(hook.tempfile, "tempdir", str(tmp_path / "no-system-temp"))
    _, result, info = hook.resub(path, j, os.stat(path), True, {2: {"rate": "25025/24000", "offset": 1.0}}, (), {2: plans["s1"], 3: plans["s2"]})
    assert result == "subtitles remuxed" and REAL_MKVMERGE(path)["attachments"] == j["attachments"], (result, info.get("warnings"))
    assert os.listdir(hook.CFG.state_dir) == [] and not os.path.exists(tmp_path / "no-system-temp")


def test_the_trim_the_damage_read_the_conversion_and_the_windows_write_nothing_to_the_system_temp_dir(mkvs, convertible, tmp_path, monkeypatch, settings):
    """The system temp dir does not exist, so a file there would fail these steps. Their work files go under STATE_DIR,
    and no work folder stays. The steps run as their own tests show."""
    monkeypatch.setattr(hook.tempfile, "tempdir", str(tmp_path / "no-system-temp"))
    for name in ("trim", "windows", "convert"):
        (tmp_path / name).mkdir()
    test_a_subtitle_trim_cuts_late_lines_and_keeps_everything_else(mkvs, tmp_path / "trim", monkeypatch, settings)
    test_windows_follow_the_video_stream_of_a_file_that_is_not_matroska(tmp_path / "windows")
    test_a_real_asf_conversion_overwrites_its_empty_temp_file(convertible, tmp_path / "convert", monkeypatch, settings)
    path, hashed, real = str(mkvs / "subtitle.mkv"), [], hook.packet_hashes
    monkeypatch.setattr(hook, "packet_hashes", lambda path, maps, bsf, text, folder, timeout: hashed.append(folder) or real(path, maps, bsf, text, folder, timeout))
    assert hook.source_read({"read": "ffmpeg"}, path, hook.mkvmerge(path)) is None   # the damage read of a clean file
    assert len(hashed) == 1 and os.path.dirname(hashed[0]) == hook.CFG.state_dir, hashed
    assert not [n for n in os.listdir(hook.CFG.state_dir) if n.startswith(tuple(f".{k}-" for k in hook.WORK_KINDS))]
    assert not os.path.exists(tmp_path / "no-system-temp")


def test_a_worker_removes_the_work_folders_a_killed_step_left(env, monkeypatch):
    """A SIGKILL leaves a work folder under STATE_DIR. A worker removes one older than a day, and one of a step that
    may still run stays."""
    for name, age in ((".resub-old", 86400 + 60), (".convert-old", 86400 + 60), (".trim-new", 60), (".keep-me", 86400 + 60)):
        os.makedirs(os.path.join(hook.CFG.state_dir, name))
        os.utime(os.path.join(hook.CFG.state_dir, name), (time.time() - age, time.time() - age))
    run_worker()
    assert {".trim-new", ".keep-me"} <= set(os.listdir(hook.CFG.state_dir)) and not {".resub-old", ".convert-old"} & set(os.listdir(hook.CFG.state_dir))


def refused_link(sync_mkv, tmp_path, monkeypatch, err=hook.errno.EPERM):
    """A copy of f.mkv with its own mode and times, a keep folder, and a file system that refuses a hard link with err."""
    path = str(tmp_path / "f.mkv")
    shutil.copy(sync_mkv / "f.mkv", path)
    os.chmod(path, 0o640)
    os.utime(path, (1_600_000_000, 1_600_000_000))
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    monkeypatch.setattr(hook.os, "link", lambda a, b: (_ for _ in ()).throw(OSError(err, os.strerror(err))))
    return path, open(path, "rb").read(), os.stat(path)


def kept_files(tmp_path):
    return sorted(os.path.relpath(os.path.join(d, n), tmp_path / ".kept") for d, _, ns in os.walk(tmp_path / ".kept") for n in ns)


@pytest.mark.parametrize("err", [hook.errno.EPERM, hook.errno.EXDEV])
def test_a_refused_hard_link_keeps_a_verified_copy_of_the_original(sync_mkv, tmp_path, monkeypatch, capsys, err):
    """A container that blocks link(), or a keep folder on another file system, refuses the hard link. The original is
    copied with its mode and times, compared, and kept, and then the fix runs. The report says it was copied."""
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch, err)
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})
    kept, k = info["kept"], os.stat(info["kept"])
    assert result == "subtitles remuxed" and open(kept, "rb").read() == before and open(path, "rb").read() != before, result
    assert info["kept_how"] == f"copied, because the hard link failed: {os.strerror(err)}" and kept_files(tmp_path) == [os.path.relpath(kept, tmp_path / ".kept")]
    assert (k.st_ino, k.st_nlink, k.st_mode, k.st_mtime) == (k.st_ino, 1, st.st_mode, st.st_mtime) and k.st_ino != os.stat(path).st_ino
    text = hook.sub_time_report({"result": "no change", "label": "f", "path": path, "subremux": dict(info, done=True)}, "done")
    assert kept in text and info["kept_how"] in text, text


def test_a_refused_hard_link_with_no_room_for_a_copy_leaves_the_file(sync_mkv, tmp_path, monkeypatch):
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch)
    monkeypatch.setattr(hook.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=st.st_size + hook.KEEP_COPY_FREE - 1))
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})
    assert result.startswith("subtitle remux failed: the original could not be kept, so it stays: the hard link failed (Operation not permitted)"), result
    assert "too little for a copy" in result and open(path, "rb").read() == before and kept_files(tmp_path) == []


def test_a_copy_that_does_not_match_the_original_is_never_kept(sync_mkv, tmp_path, monkeypatch):
    """A write went wrong, so the copy holds a byte more. The fix does not run, and no file looks kept."""
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch)
    real = os.fsync

    def fsync(fd):
        if os.readlink(f"/proc/self/fd/{fd}").endswith(".copying"):
            os.write(fd, b"x")
        return real(fd)
    monkeypatch.setattr(hook.os, "fsync", fsync)
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})
    assert result.startswith("subtitle remux failed: the original could not be kept, so it stays: the copy of the original does not match it"), result
    assert open(path, "rb").read() == before and kept_files(tmp_path) == []


def test_the_prune_removes_a_kept_copy_as_it_removes_a_kept_link(sync_mkv, tmp_path, monkeypatch):
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch)
    note = {}
    kept = hook.keep_original(path, note)
    os.rename(os.path.dirname(kept), tmp_path / ".kept" / "20000101T000000Z")   # a keep from long ago
    assert note["kept_how"].startswith("copied") and hook.prune_originals(str(tmp_path / ".kept")) == ["20000101T000000Z"] and kept_files(tmp_path) == []


def test_a_crash_while_the_original_is_copied_leaves_no_file_that_looks_kept(sync_mkv, tmp_path, monkeypatch):
    """SIGTERM lands when the copy is written but not yet compared. Until then the copy has a hidden name, and it goes."""
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch)
    real, seen = os.fsync, []

    def fsync(fd):
        name = os.readlink(f"/proc/self/fd/{fd}")
        if name.endswith(".copying"):
            seen.append(kept_files(tmp_path))
            raise SystemExit(143)
        return real(fd)
    monkeypatch.setattr(hook.os, "fsync", fsync)
    with pytest.raises(SystemExit):
        hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})
    assert len(seen) == 1 and [os.path.basename(n) for n in seen[0]] == [".f.mkv.copying"], seen
    assert open(path, "rb").read() == before and kept_files(tmp_path) == []


def test_the_flash_check_reads_no_track_once_stop_is_true(pic_mkv, monkeypatch):
    """An import stops at its deadline, and the deep analysis for a waiting import. Each track waits on stop()."""
    path = str(pic_mkv / "flash.mkv")
    j, reads, real = REAL_MKVMERGE(path), [], hook.subtitle_cues
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False: reads.append(sorted(want)) or real(path, j, want, full))
    assert sorted(hook.flash_check(path, j, [], stop=lambda: len(reads) >= 1)) == ["s1"] and reads == [["s1"]]


def test_a_track_whose_cues_lack_a_duration_is_read_for_the_flash_check(pic_mkv, monkeypatch):
    """An entry with no CueDuration hides the track's median, so the check reads the blocks and times them."""
    path = str(pic_mkv / "flash.mkv")
    real = hook.arr_decide.cue_blocks
    monkeypatch.setattr(hook.arr_decide, "cue_blocks", lambda b, tracks, cap: {n: [(c, r, None if k == 5 else d) for k, (c, r, d) in enumerate(v)]
                                                                                 for n, v in real(b, tracks, cap).items()})
    lens = hook.cue_lengths(path, REAL_MKVMERGE(path), {"s1", "s2"})
    assert lens["s1"][5] is None and lens["s1"][:5] == [0.138] * 5, lens
    plans = hook.flash_check(path, REAL_MKVMERGE(path), [])
    assert sorted(plans) == ["s1", "s2"] and plans["s1"][0][2:] == (2.138, 3.917), plans


def test_an_unindexed_track_that_reads_back_empty_gets_no_cues(noidx_mkv, monkeypatch):
    """The whole-file read gives no cue for s2. The flash check, the reference timing and the match check pass it."""
    path = str(noidx_mkv / "unindexed.mkv")
    j = REAL_MKVMERGE(path)
    monkeypatch.setattr(hook, "full_read", lambda path, j: {"cues": {"s1": talk.cues(talk.RIGHT), "s2": [], "s4": []}, "why": None})
    assert hook.flash_check(path, j, [], full=True) == {}
    assert list(hook.subtitle_cues(path, j, {"s1", "s2"}, full=True)) == ["s1"] and hook.picture_cues(path, j, {"s4"}, full=True) == {}


def test_a_flash_track_that_is_also_late_gets_its_shift_and_its_ends_in_one_remux(env, monkeypatch):
    """Each cue shows 0.14 s, and the track runs 2 s late. The check judges the cues by their new ends, so under 1 in 4
    falling in their spans no longer blocks the shift. The one remux takes the shift and the new ends."""
    english_film(env, ("eng", False, {}))
    cs = [(a, round(a + 0.14, 3), t) for a, _, t in talk.cues(talk.RIGHT, offset=2.0)]
    hearing(env, monkeypatch, {"s1": cs})
    monkeypatch.setattr(hook, "cue_lengths", lambda path, j, want, full=False: {"s1": [0.14] * len(cs)} if "s1" in want else {})
    got = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: got.append((fixes, ends)) or ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    t = decided(env)["subcheck"]["s1"]["timing"]
    (fixes, ends), = got
    assert near_fix(t["fix"], 2.0) and list(fixes) == list(ends) and near_fix(list(fixes.values())[0], 2.0), (t, got)


def test_a_flash_track_that_leaves_the_file_gets_no_new_ends(env, monkeypatch, settings):
    """The English track holds another episode's lines, and its cues flash. It leaves the file, so the remux gives it
    no new ends."""
    english_film(env, ("eng", False, {}))
    cs = [(a, a + 0.138, t) for a, _, t in talk.cues(talk.OTHER)]
    hearing(env, monkeypatch, {"s1": cs})
    monkeypatch.setattr(hook, "cue_lengths", lambda path, j, want, full=False, stop=None: {"s1": [0.138] * len(cs)} if "s1" in want else {})
    settings(keep_days=7)
    got = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: got.append((list(drop), ends)) or ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    assert got == [([2], {})] and decided(env)["flash"]["s1"]["lengthened"] == len(cs), got


@pytest.mark.parametrize("timing", [{"fix": None, "piecewise": True, "offsets": [0.1, 2.1], "why": "the cues are off, which no ratio explains"},
                                    {"fix": None, "unfixed": 2.0, "why": "the ratios 1/1, 1000/1001 fit and move the cues apart"},
                                    {"fix": None, "confirm": {"rate": "1001/960"}, "why": "a fix waits for a middle window"},
                                    {"fix": None, "few": [100.0], "why": "under two windows hold 3 cues whose first words matched"}])
def test_a_match_with_unsure_times_is_no_reference(env, monkeypatch, timing):
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)},
                        sync={"s1": dict(IN_TIME, timing=timing)})
    hook.main(["--sub-time", env["path"]])
    r = decided(env)["subtime"]["s2"]
    assert got == [] and r["verdict"] == "unknown" and r["why"].startswith("no track or sidecar of the file matched"), r


def test_a_forced_sidecar_stays_out_of_the_reference_timing(env, monkeypatch):
    sub_time_film(env, monkeypatch, [("eng", False, {})], {"s1": REF}, sides={".fr.forced.srt": REF, ".fr.srt": REF})
    hook.main(["--sub-time", env["path"]])
    names = sorted(decided(env)["subtime"])
    assert names == ["Film A (1979) WEBDL-1080p.fr.srt"], names


# --- subtitle tracks the Cues do not index, WebVTT flash and a clean sweep ------------------------------------------

@pytest.fixture(scope="module")
def noidx_mkv(pic_mkv):
    """unindexed.mkv holds a SubRip, an ASS, a WebVTT and a PGS track whose blocks no cue entry indexes, as old
    mkvmerge versions wrote them, and vobsub.mkv the VobSub track of vob.mkv the same way. ffmpeg reads mkvmerge's
    WebVTT codec id as unknown."""
    d = pic_mkv
    (d / "w.vtt").write_text("WEBVTT\n\n" + "".join(f"00:00:{2 * i:02d}.000 --> 00:00:{2 * i:02d}.120\nvtt line {i}\n\n" for i in range(1, 19)))
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "unindexed.mkv"), str(d / "av.mp4"), "--cues", "0:none", str(d / "s.srt"), "--cues", "0:none",
              str(d / "f.ass"), "--cues", "0:none", str(d / "w.vtt"), "--cues", "0:none", str(d / "plain.sup")], check=True)
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "vobsub.mkv"), "--cues", "2:none", str(d / "vob.mkv")], check=True)
    return d


def test_full_read_takes_the_tracks_the_cues_do_not_index_in_one_pass(noidx_mkv, monkeypatch, settings, tmp_path):
    """One ffmpeg pass at the lowest priority. It writes each track into a pipe, and nothing into STATE_DIR or the
    system temp dir."""
    path = str(noidx_mkv / "unindexed.mkv")
    j = REAL_MKVMERGE(path)
    assert hook.cue_less(path, j) == {"s1", "s2", "s3", "s4"} and hook.subtitle_cues(path, j, {"s1", "s2", "s3"}) == {}
    (tmp_path / "state").mkdir(); (tmp_path / "systemp").mkdir()
    settings(state_dir=str(tmp_path / "state"))
    monkeypatch.setattr(hook.tempfile, "tempdir", str(tmp_path / "systemp"))
    monkeypatch.setattr(hook, "FULL", {})
    runs, real = [], hook.subprocess.Popen
    monkeypatch.setattr(hook.subprocess, "Popen", lambda argv, **k: runs.append(argv) or real(argv, **k))
    got = hook.subtitle_cues(path, j, {"s1", "s2", "s3"}, full=True)
    assert got["s1"][0] == (2.0, 3.5, talk.RIGHT[1]) and got["s2"][0] == (2.0, 2.14, "{\\i1}Line 1{\\i0}\\Nnext"), got
    assert "s3" not in got   # ffmpeg reads mkvmerge's S_TEXT/WEBVTT as an unknown codec, so no read takes it
    assert [(a, b) for a, b, _ in hook.picture_cues(path, j, {"s4"}, full=True)["s4"]] == PIC_CUES
    assert hook.cue_lengths(path, j, {"s2"}, full=True) == {"s2": [pytest.approx(0.14)] * 20}
    ffmpeg = [a for a in runs if "ffmpeg" in a]
    assert len(ffmpeg) == 1 and ffmpeg[0][:5] == ["ionice", "-c3", "nice", "-n", "19"] and ffmpeg[0].count("-map") == 3, ffmpeg   # one pass for all
    assert len([a for a in ffmpeg[0] if a.startswith("pipe:")]) == 3, ffmpeg
    assert os.listdir(tmp_path / "state") == [] and os.listdir(tmp_path / "systemp") == []
    path = str(noidx_mkv / "vobsub.mkv")
    got = hook.picture_cues(path, REAL_MKVMERGE(path), {"s1"}, full=True)["s1"]
    assert [a for a, _, _ in got] == [a for a, _ in PIC_CUES] and all(abs(b - a - 1.5) < 0.002 for a, b, _ in got), got[:2]


def test_a_whole_file_read_of_a_file_with_no_statistics_tags_needs_no_free_space(noidx_mkv, monkeypatch, settings, tmp_path):
    """The read keeps only the cues as ffmpeg streams them. So a file with no statistics tags reads with no free space
    anywhere, writes nothing, and leaves no pipe open."""
    path = str(tmp_path / "nostats.mkv")
    shutil.copy(noidx_mkv / "unindexed.mkv", path)
    REAL_RUN(["mkvpropedit", "-q", path, "--delete-track-statistics-tags"], check=True)
    j = REAL_MKVMERGE(path)
    assert not any("tag_number_of_bytes" in t["properties"] for t in j["tracks"]) and hook.cue_less(path, j) == {"s1", "s2", "s3", "s4"}
    (tmp_path / "state").mkdir(); (tmp_path / "systemp").mkdir()
    settings(state_dir=str(tmp_path / "state"))
    monkeypatch.setattr(hook.tempfile, "tempdir", str(tmp_path / "systemp"))
    monkeypatch.setattr(hook.shutil, "disk_usage", lambda p: types.SimpleNamespace(total=0, used=0, free=0))
    monkeypatch.setattr(hook, "FULL", {})
    kept, real = [], hook.full_cues
    monkeypatch.setattr(hook, "full_cues", lambda data, fmt: (kept.append(data) if fmt == "sup" else None) or real(data, fmt))
    fds = len(os.listdir("/proc/self/fd"))
    got = hook.full_read(path, j)
    assert got["why"] is None and sorted(got["cues"]) == ["s1", "s2", "s4"] and [(a, b) for a, b, _ in got["cues"]["s4"]] == PIC_CUES, got
    assert len(kept) == 1 and [x[10] for x in sup_segments(kept[0])] == [0x16] * 2 * len(PIC_CUES), kept   # only the PCS stay
    assert os.listdir(tmp_path / "state") == [] and os.listdir(tmp_path / "systemp") == [] and len(os.listdir("/proc/self/fd")) == fds


def test_a_failed_full_read_says_why(noidx_mkv, monkeypatch, settings, tmp_path):
    path = str(noidx_mkv / "unindexed.mkv")
    settings(state_dir=str(tmp_path))
    monkeypatch.setattr(hook, "FULL", {})
    real = hook.subprocess.Popen
    monkeypatch.setattr(hook.subprocess, "Popen", lambda argv, **k: real(["sh", "-c", "echo Invalid data >&2; exit 1"] if "ffmpeg" in argv else argv, **k))
    got = hook.full_read(path, REAL_MKVMERGE(path))
    assert got["cues"] == {} and got["why"].startswith("the whole-file read failed: RuntimeError: ffmpeg exited 1: Invalid data"), got


def sup_segments(data):
    """The segments of a sup stream: "PG", PTS and DTS, type, size and data."""
    out, k = [], 0
    while k < len(data):
        out.append(data[k:k + 13 + int.from_bytes(data[k + 11:k + 13], "big")]); k += len(out[-1])
    return out


def test_sup_pcs_keeps_the_pcs_of_whole_segments_at_any_pipe_chunk():
    """The read feeds each PGS track's sup stream to sup_pcs() in pipe chunks. Any cut gives the cues of the whole
    stream, only the PCS segments stay, and at most one part segment waits. A lost sync raises."""
    whole = pgs_sup(PIC_CUES, fade={2})
    segs = sup_segments(whole)
    for size in (1, 5, 13, 64, len(whole)):
        buf, kept = bytearray(), b""
        for k in range(0, len(whole), size):
            buf += whole[k:k + size]
            kept += hook.sup_pcs(buf)
            assert len(buf) < max(map(len, segs)), (size, k)
        assert buf == b"" and kept == b"".join(x for x in segs if x[10] == 0x16) and hook.full_cues(kept, "sup") == hook.full_cues(whole, "sup"), size
    assert hook.sup_pcs(bytearray(b"P")) == b""
    for bad in (b"X", b"PX", segs[0] + b"XG" + segs[1]):
        with pytest.raises(ValueError):
            hook.sup_pcs(bytearray(bad))


def test_a_whole_file_read_stops_ffmpeg_and_closes_its_pipes_on_a_lost_sync_or_out_of_time(noidx_mkv, monkeypatch):
    """A lost sync fails the read and says why. The job's time limit passes on. Either way ffmpeg stops and no pipe
    stays open."""
    path = str(noidx_mkv / "unindexed.mkv")
    j, procs, real = REAL_MKVMERGE(path), [], hook.subprocess.Popen
    fake = lambda argv: ["bash", "-c", f"printf XXXX >&{argv[-1][5:]}; exec sleep 60"]   # s4, the PGS track, is the last output
    monkeypatch.setattr(hook.subprocess, "Popen", lambda argv, **k: procs.append(real(fake(argv) if "ffmpeg" in argv else argv, **k)) or procs[-1])
    monkeypatch.setattr(hook, "FULL", {})
    fds = len(os.listdir("/proc/self/fd"))
    got = hook.full_read(path, j)
    assert got["cues"] == {} and got["why"] == "the whole-file read failed: ValueError: a PGS track's sup stream lost its sync", got
    assert procs[-1].returncode == -signal.SIGKILL and len(os.listdir("/proc/self/fd")) == fds

    def up(*a):
        raise hook.arr_meta.OutOfTime()
    monkeypatch.setattr(hook, "FULL", {})
    monkeypatch.setattr(hook, "sup_pcs", up)
    with pytest.raises(hook.arr_meta.OutOfTime):
        hook.full_read(path, j)
    assert procs[-1].returncode == -signal.SIGKILL and len(os.listdir("/proc/self/fd")) == fds


def test_an_import_skips_a_track_the_cues_do_not_index_and_says_why(env, monkeypatch):
    """An import never reads the whole file. --sub-check does, so a backfill passes full to the readers."""
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {})
    monkeypatch.setattr(hook, "cue_less", lambda path, j: {"s1"})
    fulls = []
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False, stop=None: fulls.append(full) or {})
    hook.main([])
    rec = decided(env)
    assert rec["unindexed"]["tracks"] == ["s1"] and "an import never reads the whole file" in rec["subcheck"]["s1"]["why"] and not any(fulls), rec
    fulls.clear()
    hook.process(hook.Ctx("radarr", env["path"], "Film A (1979)", "English", 120, mode="sub_check", apply=False, post=False))
    assert fulls and all(fulls)


def test_a_webvtt_flash_track_only_reports(env, monkeypatch):
    got = flash_film(env, monkeypatch)
    env["probe"]["tracks"][2]["properties"]["codec_id"] = "S_TEXT/WEBVTT"
    hook.main([])
    rec = decided(env)
    assert got == [] and rec["flash"]["s1"]["report_only"] == "WebVTT" and rec["alert_kinds"] == [], rec
    assert "s1 flashes, 40 of 40 ends need a fix, WebVTT: report only" in hook.sub_text(rec, "done")


@pytest.mark.parametrize("off, fixed", [(0.3, True), (1.0, False)])
def test_a_clean_sweep_makes_a_match_with_too_few_anchors_a_reference(env, monkeypatch, off, fixed):
    """The English track matched, but too few of its cues anchored a fix. Its sweep heard ten windows in time, so it
    times the French track. With one window 1 s off, the sweep is not clean and there is no reference."""
    few = dict(IN_TIME, timing={"fix": None, "few": [100.0], "why": "under two windows hold 3 cues whose first words matched"})
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)}, sync={"s1": few})
    rows = [{"at": 60.0 + 100 * k, "words": 15, "overlap": 0.9, "cues": 4, "offset": off if k == 4 else 0.1, "off": 0.1} for k in range(10)]
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({"s1": rows}, {"cpu": 0, "took": 0, "failed": []}))
    hook.main(["--sub-time", env["path"]])
    rec = decided(env)
    assert bool(got) == fixed and rec.get("references") == ({"s1": "clean sweep"} if fixed else {}), (got, rec.get("references"))


# --- the deep analysis after an import (docs/design.md, "How it runs" and "Subtitle match") ------------------------

INPUTS = {"label": "Film A (1979)", "original": "English", "runtime": 120, "want": None, "kids": False, "ctx": {"listed": 120}, "release": ""}


def queue_analysis(env, path, age=0.0, owner="7", **inputs):
    """A deep analysis job for path, the way an import queues one, age seconds old, with the import's inputs."""
    rec = {"path": path, "ids": {"app_id": owner, "file_id": None}, "tracks": [{"i": "s1"}]}
    name = hook.queue_deep_analysis({"app": "radarr", "owner": owner}, rec, dict(INPUTS, **inputs))
    data = hook.job_of(name)
    hook.store.write("UPDATE jobs SET job = ?, at = ? WHERE name = ? AND claimed = 0", json.dumps(dict(data, time=data["time"] - age)),
                     time.time() - age, name)
    return name


@pytest.mark.parametrize("workers", [1, 3])
def test_imports_go_first_and_the_deep_analysis_drains_while_idle(pool, monkeypatch, settings, workers):
    """A deep analysis job two days old waits behind an import and still runs: it never drops by age. The worker runs
    it with no new event, and stops once both queues are empty."""
    settings(hook_workers=workers, subtitles="deep")
    a, b, c = films(pool, 3)
    queue_analysis(pool, a, age=2 * 86400, owner="1")
    enqueue(pool, 0, b, owner="2")
    enqueue(pool, 1, c, owner="3")
    real = hook.process
    monkeypatch.setattr(hook, "process", lambda ctx: trace("process", path=ctx.path, source=ctx.source) or real(ctx))
    run_worker()
    order = [(r["path"], r["source"]) for r in traced("process")]
    assert order[0][1] == "hook" and (a, "deep_analysis") in order and order.index((a, "deep_analysis")) > 0, order
    if workers == 1:   # both imports before any deep analysis, which never starts only to yield
        assert order[:2] == [(b, "hook"), (c, "hook")] and order[2] == (a, "deep_analysis"), order
        assert not [r for r in log_lines(pool) if r.get("result") == "yielded"]
    assert hook.queued() == [] and not claimed() and set(finals(pool)) == {a, b, c}, finals(pool)
    assert hook.deep_analysis_queued() == [] and (b, "deep_analysis") in order, "the deep analysis the import of b queued ran too"


def test_a_re_import_replaces_the_queued_deep_analysis(env, monkeypatch, settings):
    settings(subtitles="deep")
    first = queue_analysis(env, env["path"], age=100)
    second = queue_analysis(env, env["path"])
    assert first == second and hook.deep_analysis_queued() == [first]
    for level in ("off", "check", "fix"):   # only deep queues it, and fix is the default
        settings(subtitles=level)
        assert hook.queue_deep_analysis({"app": "radarr"}, {"path": env["path"], "tracks": [{"i": "s1"}]}, INPUTS) is None, level


def test_the_deep_analysis_queue_runs_the_job_queued_longest_ago_first(env, monkeypatch, settings):
    settings(subtitles="deep")
    paths = []
    for k in range(3):
        paths.append(os.path.join(os.path.dirname(env["path"]), f"Film {k}.mkv"))
        shutil.copy(env["path"], paths[-1])
    names = [queue_analysis(env, p, age=age) for p, age in zip(paths, (100, 300, 200))]
    assert hook.deep_analysis_queued() == [names[1], names[2], names[0]]


def test_an_import_preempts_the_deep_analysis_between_two_sweep_groups(env, monkeypatch, settings):
    """The sweep yields after its first group, because an import arrived. The deep analysis job stays queued, the
    worker runs the import, and the next run of the deep analysis hears the rest."""
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    real, calls = hook.lid_run, []

    def lid_run(path, index, j, expect, timeout, fresh=False, keep=False, words=None, then=None, yield_to=None):
        got = real(path, index, j, expect, timeout, fresh, keep, words, then, yield_to)
        if words and len(words) > 4 and words[4]:   # the sweep
            calls.append((len(words[1]), yield_to))
            if len(calls) == 1:   # an import arrives during the first group
                enqueue(env, 5, env["path"])
                return dict(got, windows=got["windows"][:2], yielded=True)
        return got
    monkeypatch.setattr(hook, "lid_run", lid_run)
    name = queue_analysis(env, env["path"])
    order = []
    monkeypatch.setattr(hook, "run_job", lambda n, pending, **k: order.append("import") or hook.drop_job(n))
    real_deep = hook.deep_analysis
    monkeypatch.setattr(hook, "deep_analysis", lambda n, pending, claimed=False: order.append("deep") or real_deep(n, pending, claimed))
    run_worker()
    assert order == ["deep", "import", "deep"] and hook.deep_analysis_queued() == [], order
    assert calls[0][1] == (os.path.join(hook.CFG.state_dir, "lid.turn.gate"), hook.store.path()) and calls[1][0] == calls[0][0], calls   # the cache holds the pair heard
    yielded = [r for r in log_lines(env) if r.get("result") == "yielded"]
    assert len(yielded) == 1 and yielded[0]["job"] == name and "an import waits, after 2 of" in yielded[0]["note"], yielded
    rec = decided(env)
    assert rec["source"] == "deep_analysis" and len(rec["sweep"]["s1"]) == calls[0][0] and "tmdb" not in rec, rec


def test_an_import_times_other_tracks_against_a_proven_reference_and_queues_the_deep_analysis(env, monkeypatch, settings):
    """Stage 1: the English track matched in time in its two windows, so the import fixes the French track 2 s late.
    It hears no sweep and reads no whole file. The deep analysis of the file is queued for the rest."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: pytest.fail("an import hears no sweep"))
    monkeypatch.setattr(hook, "full_read", lambda *a, **k: pytest.fail("an import reads no whole file"))
    queued = []
    monkeypatch.setattr(hook, "deep_analysis", lambda n, pending, claimed=False: queued.append(n) or hook.drop_job(n, claimed))
    hook.main([])
    rec = decided(env)
    (apply, fixes, drop), = got
    assert apply and list(fixes) == [3] and near_fix(fixes[3], 2.0) and rec["references"] == {"s1": "in time"}, got
    assert queued == [f"deep-analysis-{hashlib.sha1(env['path'].encode()).hexdigest()[:16]}.json"], queued


@pytest.mark.parametrize("codec, reader", [("S_HDMV/PGS", "picture_cues"), ("S_TEXT/UTF8", "subtitle_cues")])
def test_an_import_defers_what_it_has_no_time_to_read_or_fit(env, monkeypatch, settings, codec, reader):
    """The job's time limit leaves 50 s over SUB_RESERVE. The file holds 40 Spanish tracks, PGS or text. Each read takes
    15 s and each fit 12 s, and a track is fitted right after its read. Two tracks are read and fitted before the time
    runs out, and the other 38 wait unread. The import still ends with its flags decided, and it queues the deep
    analysis."""
    settings(subtitles="deep")
    late = [(a + 2.0, b + 2.0, "") for a, b, _ in REF]
    got = sub_time_film(env, monkeypatch, [("eng", False, {})] + [("spa", False, {"codec_id": codec})] * 40, {"s1": REF})
    tick = lambda s: env["clock"].__setitem__(0, env["clock"][0] + s)

    def read(path, j, want, full=False):
        out = {}
        for p in sorted(want):
            if p == "s1":   # the word check's own read of the English track
                out[p] = REF
                continue
            tick(15)
            reads.append(p)
            out[p] = late
        return out
    reads = []
    monkeypatch.setattr(hook, reader, read)
    real = hook.arr_subsync.reference
    monkeypatch.setattr(hook.arr_subsync, "reference", lambda *a, **k: tick(12) or real(*a, **k))
    monkeypatch.setattr(hook.DEADLINE, "left", lambda: hook.SUB_RESERVE + 50)
    queued = []
    monkeypatch.setattr(hook, "deep_analysis", lambda n, pending, claimed=False: queued.append(n) or hook.drop_job(n, claimed))
    hook.main([])
    rec = decided(env)
    assert [rec["subtime"][f"s{k}"]["verdict"] for k in range(2, 42)] == ["fit", "fit"] + ["deferred"] * 38, rec["subtime"]
    assert reads == ["s2", "s3"] and rec["outcome"] == "no_change" and got and list(got[0][1]) == [3, 4] and len(queued) == 1, (reads, rec["outcome"], got)


def test_a_read_that_crosses_the_deadline_defers_its_fit_and_the_sidecars(env, monkeypatch, settings):
    """40 s over SUB_RESERVE. The first PGS track is read and fitted in 27 s. The second read starts in time and ends at
    42 s, past the deadline, so its fit waits for the deep analysis. So does the French sidecar."""
    settings(subtitles="deep")
    late = [(a + 2.0, b + 2.0, "") for a, b, _ in REF]
    got = sub_time_film(env, monkeypatch, [("eng", False, {})] + [("spa", False, {"codec_id": "S_HDMV/PGS"})] * 3, {"s1": REF},
                        sides={".fr.srt": talk.moved_to(REF, offset=2.0)})
    tick, reads = lambda s: env["clock"].__setitem__(0, env["clock"][0] + s), []
    monkeypatch.setattr(hook, "picture_cues", lambda path, j, want, full=False: [tick(15), reads.extend(sorted(want))] and {p: late for p in want})
    real = hook.arr_subsync.reference
    monkeypatch.setattr(hook.arr_subsync, "reference", lambda *a, **k: tick(12) or real(*a, **k))
    monkeypatch.setattr(hook.DEADLINE, "left", lambda: hook.SUB_RESERVE + 40)
    monkeypatch.setattr(hook, "deep_analysis", lambda n, pending, claimed=False: hook.drop_job(n, claimed))
    hook.main([])
    r = decided(env)["subtime"]
    side = os.path.basename(env["path"])[:-4] + ".fr.srt"
    assert reads == ["s2", "s3"] and [r[p]["verdict"] for p in ("s2", "s3", "s4", side)] == ["fit", "deferred", "deferred", "deferred"], (reads, r)
    assert got and list(got[0][1]) == [3], got


def test_sub_check_reads_the_whole_file_and_hears_no_sweep(env, monkeypatch):
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: pytest.fail("--sub-check hears no sweep"))
    hook.main(["--backfill", "radarr", "--sub-check"])
    assert got and list(got[0][1]) == [3], got


@pytest.mark.parametrize("argv", [["--sub-time", None], ["--backfill", "radarr", "--sub-check"]])
def test_the_whole_file_read_runs_under_the_file_lock(env, monkeypatch, argv):
    """--sub-time and --sub-check read the cues, and a track the Cues do not index from the whole file, while they
    hold the file lock. Only the hearing runs without it."""
    sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": REF})
    ops, reads = [], []
    real = hook.fcntl.flock
    monkeypatch.setattr(hook.fcntl, "flock", lambda f, op: (ops.append(op) if f.name.endswith("/lock") else None) or real(f, op))
    fake = hook.subtitle_cues
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False: reads.append((full, bool(ops) and not ops[-1] & hook.fcntl.LOCK_UN))
                        or fake(path, j, want, full))
    hook.main([x or env["path"] for x in argv])
    assert len(reads) >= 2 and all(full and held for full, held in reads), reads


@pytest.mark.parametrize("changed", [False, True])
def test_a_deep_analysis_after_a_yield_reads_the_whole_file_once(env, monkeypatch, settings, changed):
    """The whole-file read of a film takes minutes. A deep analysis that yielded finds it in its .read file, unless the
    file changed since, by its size or mtime."""
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    reads = []
    monkeypatch.setattr(hook, "full_read", lambda path, j: reads.append(path) or {"cues": {}, "tracks": [], "took": 0, "cpu": 0, "why": None})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: (_ for _ in ()).throw(hook.Yielded("an import waits")))
    name = queue_analysis(env, env["path"])
    hook.deep_analysis(name, [])
    monkeypatch.setattr(hook, "FULL", {})   # a new job process
    if changed:
        with open(env["path"], "ab") as f:
            f.write(b"y")
    hook.deep_analysis(name, [])
    assert reads == [env["path"]] * (2 if changed else 1) and hook.deep_analysis_queued() == [name], reads


def test_an_import_with_no_reference_reads_no_other_track(env, monkeypatch):
    """Nothing matched, so no track can be timed: the import reads none of them."""
    unmatched = dict(IN_TIME, verdict="unknown", why="2 of 2 windows hold under 8 heard words", timing=None)
    sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {}), ("spa", False, {"codec_id": "S_HDMV/PGS"})], {"s1": REF, "s2": REF},
                  sync={"s1": unmatched})
    reads = []
    real = hook.subtitle_cues
    monkeypatch.setattr(hook, "subtitle_cues", lambda path, j, want, full=False, stop=None: reads.append(sorted(want)) or real(path, j, want, full))
    monkeypatch.setattr(hook, "picture_cues", lambda *a, **k: pytest.fail("no picture track is read with no reference"))
    hook.main([])
    assert reads == [["s1"]] and "subtime" not in decided(env), reads   # the word check's own read only


def test_a_deep_analysis_reads_the_whole_file_again_when_it_changed_before_the_lock(env, monkeypatch, settings):
    """The deep analysis reads the whole file before it takes the file lock. An edit lands after that read, so the
    file's size and mtime differ, and the read runs again under the lock. The .read file goes at the end."""
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    monkeypatch.setattr(hook, "cue_less", lambda path, j: {"s1"})
    monkeypatch.setattr(hook, "ff_streams", lambda path: ("matroska", [{"codec_type": "subtitle", "codec_name": "subrip"}], 600.0))
    env["cc_text"] = srt_text(talk.cues(talk.RIGHT))
    reads, popen = [], hook.subprocess.Popen
    monkeypatch.setattr(hook.subprocess, "Popen", lambda argv, **k: popen(reads.append(os.path.getsize(env["path"])) or  # ffmpeg writes the SubRip text
                        ["bash", "-c", f'printf %s "$1" >&{argv[-1][5:]}', "bash", env["cc_text"]] if "-map" in argv else argv, **k))
    real = hook.deep_waits

    def deep_waits():   # an edit lands between the read and the lock
        if len(reads) == 1 and os.path.getsize(env["path"]) == 1000:
            with open(env["path"], "ab") as f:
                f.write(b"y")
        return real()
    monkeypatch.setattr(hook, "deep_waits", deep_waits)
    name = queue_analysis(env, env["path"])
    hook.deep_analysis(name, [])
    rec = decided(env)
    assert reads == [1000, 1001] and rec["full_read"]["tracks"] == ["s1"] and hook.deep_analysis_queued() == [], (reads, rec.get("full_read"))


def change_file(path, how):
    """Change the file at path as the app would. replaced puts another file of the same size and mtime there, so only
    the inode differs. removed deletes it."""
    if how == "replaced":
        st = os.stat(path)
        shutil.copy(path, path + ".new")
        os.utime(path + ".new", ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(path + ".new", path)
    else:
        os.remove(path)


def dropped(env, name, how, got):
    """The deep analysis job name dropped itself after the change how, with one decision line and no error. It remuxed
    nothing, posted no alert, and is gone from the queue."""
    code, result = ("file_gone", "dropped, the file is gone") if how == "removed" else ("file_replaced", "dropped, another file replaced it since its import")
    lines = [r for r in log_lines(env) if r.get("job") == name and r["result"] != "yielded"]
    assert [(r.get("outcome"), r["result"]) for r in lines] == [(code, result)] and "trace" not in lines[0], lines
    assert got == [] and not [u for m, u, b in env["http"] if m == "POST"] and hook.deep_analysis_queued() == []


@pytest.mark.parametrize("how", ["replaced", "removed"])
def test_a_deep_analysis_drops_itself_when_its_file_was_replaced_since_its_import(env, monkeypatch, settings, how):
    """An upgrade replaced the file, or it went, after the import queued the deep analysis. The job drops itself and
    reads nothing. A new import of the path queues its own deep analysis."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    name = queue_analysis(env, env["path"])
    change_file(env["path"], how)
    monkeypatch.setattr(hook, "full_read", lambda path, j: pytest.fail("a dropped job reads nothing"))
    hook.deep_analysis(name, [])
    dropped(env, name, how, got)


@pytest.mark.parametrize("when, how", [("read", "replaced"), ("read", "removed"), ("sweep", "replaced"), ("sweep", "removed"),
                                       ("yield", "replaced"), ("remux", "replaced"), ("remux", "removed"), ("error", "replaced"),
                                       ("error", "removed")])
def test_a_deep_analysis_drops_itself_when_its_file_is_replaced_during_the_run(env, monkeypatch, settings, when, how):
    """The app replaces the file during the whole-file read, which holds no lock, and the job sees it when it takes the
    lock. Or during the sweep, which runs without the lock, and the job sees it when it takes the lock exclusive, or
    at the start of its next run when the sweep yielded. The app holds no lock, so it can also replace the file during
    the remux, which then fails with "the original changed", or during a read that then raises. The job never checks
    the new file, and logs no error and no alert."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    path, done = env["path"], []
    once = lambda: done or done.append(change_file(path, how))
    if when == "read":
        monkeypatch.setattr(hook, "full_read", lambda p, j: once() or {"cues": {}, "tracks": [], "took": 0, "cpu": 0, "why": None})
    elif when == "sweep":
        monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: once() or ({}, {"cpu": 0, "took": 0, "failed": []}))
    elif when == "yield":
        monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: (_ for _ in ()).throw(hook.Yielded("an import waits")) if not once() else
                            ({}, {"cpu": 0, "took": 0, "failed": []}))
    else:
        def resub(p, *a, **k):
            change_file(p, how)
            if when == "error":
                raise FileNotFoundError(2, "No such file or directory", p)
            return "subtitle_remux_failed", "subtitle remux failed, the original changed: the app replaced or renamed the original", {"warnings": None}
        monkeypatch.setattr(hook, "resub", resub)
    name = queue_analysis(env, path)
    hook.deep_analysis(name, [])
    if when == "yield":
        hook.deep_analysis(name, [])
    dropped(env, name, how, got)


@pytest.mark.parametrize("gone", [True, False])
def test_a_file_not_found_in_a_step_drops_the_deep_analysis_only_when_its_file_is_gone(env, monkeypatch, settings, gone):
    """hd 2026-10-01 23:43: Sonarr imported an SDTV upgrade of the episode during its deep analysis, and the hook converted
    it to a .mkv of another name. A step then raised FileNotFoundError on the old WEBDL path. The job drops itself, with
    no error and no retry. A FileNotFoundError while the file is still there stays an error."""
    settings(subtitles="deep")
    sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    path = env["path"]

    def step(*a):
        if gone:   # the upgrade, converted to .mkv under its own name
            os.replace(path, path.replace("WEBDL-1080p", "SDTV"))
        raise FileNotFoundError(2, "No such file or directory", path if gone else "/usr/bin/ffprobe")
    monkeypatch.setattr(hook, "sub_findings", step)
    name = queue_analysis(env, path)
    hook.deep_analysis(name, [])
    (rec,) = [r for r in log_lines(env) if r.get("job") == name]
    if gone:
        assert (rec["outcome"], rec["result"], "trace" in rec) == ("file_gone", "dropped, the file is gone", False), rec
        assert rec["note"] == f"FileNotFoundError: [Errno 2] No such file or directory: '{path}'"
    else:
        assert (rec["outcome"], rec["result"]) == ("error", "error: FileNotFoundError: [Errno 2] No such file or directory: '/usr/bin/ffprobe'"), rec
    assert hook.deep_analysis_queued() == [] and not [u for m, u, b in env["http"] if m == "POST"]


@pytest.mark.parametrize("fails", [False, True])
def test_a_deep_analysis_goes_on_after_its_own_remux(env, monkeypatch, settings, fails):
    """The remux of the deep analysis puts another file at the path. The run still logs the remux and queues the Plex
    analyze, because the job compares the file with its own last stat. A step that fails after the remux is an error."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    fake = hook.resub
    monkeypatch.setattr(hook, "resub", lambda p, *a, **k: change_file(p, "replaced") or fake(p, *a, **k))
    if fails:
        monkeypatch.setattr(hook, "sub_findings", lambda *a: 1 / 0)
    pending = []
    hook.deep_analysis(queue_analysis(env, env["path"], want={"guids": ["tmdb://90001"], "title": "Film A", "show": False}), pending)
    rec = decided(env)
    if fails:
        assert (rec["outcome"], rec["result"], len(got), pending) == ("error", "error: ZeroDivisionError: division by zero", 1, []), rec
    else:
        assert (rec["outcome"], rec["reasons"], len(got), len(pending)) == ("no_change", ["subtitle_retimed"], 1, 1), rec


@pytest.mark.parametrize("upgrade", [False, True])
def test_a_deep_analysis_that_converted_its_file_and_yielded_goes_on_with_the_new_file(env, monkeypatch, settings, upgrade):
    """The import left a .mkv that holds MP4. The deep analysis converts it, which puts another file at the path, and
    then yields to an import. The job takes the key of the new file, so its next run goes on and remuxes. When an
    upgrade replaced the new file during the sweep, the next run drops the job."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    env["mkv_probe"] = dict(env["probe"], container=dict(env["probe"]["container"], type="Matroska"))   # the same tracks after the conversion
    mp4_named_mkv(env)
    monkeypatch.setattr(hook, "full_read", lambda p, j: {"cues": {}, "tracks": [], "took": 0, "cpu": 0, "why": None})
    sweeps = []

    def sweep(*a, **k):
        sweeps.append(1)
        if len(sweeps) == 1:
            if upgrade:
                change_file(env["path"], "replaced")
            raise hook.Yielded("an import waits")
        return {}, {"cpu": 0, "took": 0, "failed": []}
    monkeypatch.setattr(hook, "sub_sweep", sweep)
    name = queue_analysis(env, env["path"])
    hook.deep_analysis(name, [])
    assert len(env["repacks"]) == 1 and hook.deep_analysis_queued() == [name] and got == []
    hook.deep_analysis(name, [])
    if upgrade:
        return dropped(env, name, "replaced", got)
    rec = decided(env)
    assert (rec["outcome"], rec["reasons"], len(got), len(env["repacks"])) == ("no_change", ["subtitle_retimed"], 1, 1), rec
    assert hook.deep_analysis_queued() == []


def test_a_deep_analysis_decides_with_the_inputs_of_its_import(env, monkeypatch, settings):
    """The import stores its release name, original language, kids flag and the rest in the job. The deep analysis
    passes them to the decision, so a rule such as ENGLISH_RELEASE decides as it did on the import."""
    settings(subtitles="deep")
    monkeypatch.setenv("radarr_moviefile_scenename", "Film.A.1979.1080p.WEB-DL")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    real, seen = hook.process, []
    monkeypatch.setattr(hook, "process", lambda ctx: seen.append((ctx.original, ctx.runtime, ctx.release, ctx.kids, ctx.source)) or real(ctx))
    hook.main([])
    assert seen == [("English", round(talk.DURATION / 60), "Film.A.1979.1080p.WEB-DL", False, s) for s in ("hook", "deep_analysis")], seen


def test_a_deep_analysis_leaves_the_header_check_to_its_import(env, monkeypatch, settings):
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    seen, real = [], hook.header_of
    monkeypatch.setattr(hook, "header_of", lambda path, j: seen.append(path) or real(path, j))
    hook.deep_analysis(queue_analysis(env, env["path"]), [])
    assert seen == [] and [r["outcome"] for r in log_lines(env) if r.get("source") == "deep_analysis" and r.get("outcome")] == ["no_change"]


def test_a_deep_analysis_yields_before_its_remux_when_an_import_waits(env, monkeypatch, settings):
    """The French track needs a fix of 2 s. The check runs again under the exclusive lock before a remux, and an import
    arrives during its fit, so the remux waits for the next run."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    real, fits = hook.arr_subsync.reference, []
    monkeypatch.setattr(hook.arr_subsync, "reference", lambda *a, **k: (fits.append(1) or len(fits) < 2 or enqueue(env, 1, env["path"])) and real(*a, **k))
    name = queue_analysis(env, env["path"])
    hook.deep_analysis(name, [])
    yielded = [r for r in log_lines(env) if r.get("result") == "yielded"]
    assert len(fits) == 2 and got == [] and len(yielded) == 1 and hook.deep_analysis_queued() == [name], (fits, got, yielded)


def test_an_import_stores_the_inputs_of_its_decision_in_the_deep_job(env, monkeypatch, settings):
    """A kids film with an edit and an English sidecar. The deep job holds the label, kids flag, metadata context and
    Plex lookup that the import used."""
    settings(subtitles="deep")
    env["movies"]["movie/7"]["qualityProfileId"] = 8   # the Kids profile
    open(env["path"][:-4] + ".en.srt", "w").write(srt_text(REF))
    real, seen, wants, jobs = hook.process, [], [], []
    monkeypatch.setattr(hook, "process", lambda ctx: seen.append((ctx.label, ctx.kids, ctx.item)) or real(ctx))
    real_after = hook.plex_after
    monkeypatch.setattr(hook, "plex_after", lambda app, source, rec, want: wants.append(want) or real_after(app, source, rec, want))

    def deep(n, pending, claimed=False):
        jobs.append(hook.job_of(n, claimed))
        hook.drop_job(n, claimed)
    monkeypatch.setattr(hook, "deep_analysis", deep)
    hook.main([])
    got = jobs[0]["inputs"]
    json_of = lambda x: json.loads(json.dumps(x))   # the job holds JSON, so a tuple comes back as a list
    assert seen[0][1] is True and [got["label"], got["kids"], got["ctx"]] == json_of(seen[0]) and wants and got["want"] == json_of(wants[0]), (seen, got)


def test_a_deep_analysis_passes_the_stored_inputs_to_the_decision(env, monkeypatch, settings):
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    real, seen = hook.process, []
    monkeypatch.setattr(hook, "process", lambda ctx: seen.append((ctx.label, ctx.kids, ctx.item, ctx.ids)) or real(ctx))
    hook.deep_analysis(queue_analysis(env, env["path"], label="Film B (1980)", kids=True, ctx={"listed": 99}), [])
    assert seen == [("Film B (1980)", True, {"listed": 99}, {"app_id": "7", "file_id": None})], seen


def test_a_deep_remux_asks_the_app_to_rescan_its_item(env, monkeypatch, settings):
    settings(subtitles="deep")
    sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    hook.deep_analysis(queue_analysis(env, env["path"]), [])
    rec = decided(env)
    assert rec["subremux"]["done"] and rec["subremux"]["rescan"] == "sent", rec["subremux"]
    assert ("POST", "command", {"name": "RescanMovie", "movieId": 7}) in env["writes"], env["writes"]


def test_a_flag_edit_that_changes_nothing_is_never_sent(env, monkeypatch):
    """The subtitle is not default, so an edit that turns its default flag off changes nothing."""
    english_film(env, ("eng", False, {}))
    rec = hook.edit({"path": env["path"]}, env["probe"], [["track:=10", 0, 1], ["track:=10", 0, 1, "flag-forced"]], True)
    assert rec["outcome"] == "no_change" and env["mkvpropedit"] == [], rec


def sweep_rows(offsets, fit=lambda at: 0.0):
    """Sweep rows, one a minute, that heard enough, at the raw offsets, and off the line fit(at) of a fix."""
    return [{"at": 60.0 * k, "words": 15, "overlap": 0.9, "cues": 4, "offset": o, "off": round(o - fit(60.0 * k), 2)} for k, o in enumerate(offsets, 1)]


@pytest.mark.parametrize("before, after", [
    ([0.29] * 7 + [2.0] * 4, [0.0] * 6 + [0.35] * 5),   # a closer median, but fewer windows within 0.3 s
    ([0.0] * 6 + [2.0] * 5, [0.3] * 6 + [0.31] * 5)])   # as many windows within 0.3 s, but a farther median
def test_the_sweep_blocks_a_fix_that_is_worse_by_either_measure(before, after):
    rows = [dict(w, off=o) for w, o in zip(sweep_rows(before), after)]
    assert hook.sweep_confirms(rows).startswith("the sweep puts ")


def test_a_window_on_the_line_that_hears_too_little_gets_its_longer_window(env, monkeypatch):
    """A track 1.2 s late. Nobody speaks around the window at a third of the file, so its window of 24 seconds elsewhere
    in that part confirms the fix."""
    S, stop = hook.arr_subsync, hook.arr_decide.STOPWORDS["eng"]
    track = talk.cues(talk.RIGHT, offset=1.2)
    fix = {"rate": "1/1", "offset": 1.2}
    first = S.windows(track, talk.DURATION, stop)
    third = S.moved(S.windows(track, talk.DURATION, stop, taken=[S.moved(a * 1000, fix) / 1000 + 1.2 for a in first], parts=S.LINE_PARTS)[0] * 1000, fix) / 1000
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": track}, spoken=lambda i: not third - 2 <= talk.FIRST + talk.GAP * i <= third + S.WINDOW + 2)
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None: ("subtitles_remuxed", "subtitles remuxed", {"warnings": None}))
    hook.main([])
    t = decided(env)["subcheck"]["s1"]["timing"]
    assert near_fix(t["fix"], 1.2) and env["more"][-1] and t["why"].endswith("sit on its line"), (t, env["more"])


def test_the_windows_on_the_line_avoid_the_windows_heard_already():
    """A window heard already near a third of the file, as a middle window can lie. The window on the line there goes
    elsewhere in that part, so no audio is heard twice."""
    S, stop = hook.arr_subsync, hook.arr_decide.STOPWORDS["eng"]
    track = talk.cues(talk.RIGHT, offset=1.2)
    free = S.windows(track, talk.DURATION, stop, parts=S.LINE_PARTS)
    got = S.windows(track, talk.DURATION, stop, taken=[free[0]], parts=S.LINE_PARTS)
    assert abs(got[0] - free[0]) >= S.WINDOW and got[1] == free[1], (free, got)


def test_the_sweep_confirms_a_drift_fix_and_blocks_a_fix_of_a_track_in_time():
    """A drift from 0.2 s to 2.9 s early sits within 0.47 s of the fitted line, much closer than to the audio: the fix
    stands. A track whose windows sit within 0.31 s of the audio moves up to 0.94 s off after a fix of 0.87 s and a
    ratio: the fix goes. So does a fix the sweep heard too little to judge."""
    drift = [-0.2 - 2.7 * k / 20 for k in range(21)]
    line = lambda at: -0.2 - 2.7 * (at / 60 - 1) / 20
    rows = sweep_rows(drift, line)
    for k in (3, 9, 15):   # the windows of a real drift wander around the line
        rows[k]["off"] = 0.47 if k != 9 else -0.4
    assert hook.sweep_confirms(rows) is None
    right = [0.05 if k % 2 else -0.1 for k in range(21)]
    right[20] = 0.31
    wrong_line = lambda at: -0.87 + (at / 60) * 0.1   # the line of +0.87 s and 1000/1001 after the fix
    assert hook.sweep_confirms(sweep_rows(right, wrong_line)).startswith("the sweep puts ")
    assert hook.sweep_confirms(sweep_rows([0.1, 0.2])) == "the sweep heard 2 windows to judge the fix by, under 3, so the times stay"


@pytest.mark.parametrize("how", ["sub-time", "deep"])
def test_a_fix_that_the_sweep_does_not_confirm_is_never_applied(env, monkeypatch, settings, how):
    """The word check asks for +0.87 s and 1000/1001 from two windows, as on a track whose late window hit a short
    patch. The sweep hears the track in time, so no remux runs. Its clean sweep makes it a reference, and its rows
    sit off the audio, as no line is fitted."""
    settings(subtitles="deep")
    fixed = dict(IN_TIME, timing={"fix": {"rate": "1000/1001", "offset": 0.87}, "why": "a fix of +0.87 s and the ratio 1000/1001"})
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": REF}, sync={"s1": fixed})
    rows = sweep_rows([0.05, -0.05] * 8, lambda at: 0.87 - 0.001 * at)
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({"s1": copy.deepcopy(rows)}, {"cpu": 0, "took": 0, "failed": []}))
    if how == "deep":
        hook.deep_analysis(queue_analysis(env, env["path"]), [])
    else:
        hook.main(["--sub-time", env["path"], "--apply"])
    rec = decided(env)
    t = rec["subcheck"]["s1"]["timing"]
    assert got == [] and t["fix"] is None and t["unconfirmed"] == fixed["timing"]["fix"] and t["swept"] and "the sweep puts" in t["why"], t
    assert rec["references"] == {"s1": "clean sweep"} and [w["off"] for w in rec["sweep"]["s1"]] == [w["offset"] for w in rows], rec.get("references")
    assert "subtiming" not in rec.get("alert_kinds", []), rec.get("alerts")


def test_sweep_offset_is_the_median_and_not_the_mean():
    assert hook.sweep_offset([{"words": 15, "offset": x} for x in (0.4, 0.5, 2.0)]) == 0.5


def test_a_sweep_row_with_too_few_cues_never_splits_a_step():
    row = lambda at, off, cues=4: {"at": at, "words": 20, "overlap": 0.9, "cues": cues, "offset": off, "off": off}
    rows = [row(60, 0.1), row(120, 1.2), row(180, 0.2, 2), row(240, 1.3), row(300, 0.1)]
    assert hook.sweep_steps(rows) == {id(rows[1]), id(rows[3])}


@pytest.mark.parametrize("ref, says", [("s1", "the subtitles (track 1)"), ("Film.en.srt", "the subtitles in Film.en.srt")])
def test_the_alert_names_a_reference_sidecar_as_a_sidecar(ref, says):
    sync = {"s2": {"reference": ref, "timing": {"fix": None, "piecewise": True, "offsets": [0.1, 2.1], "why": "the cues are off"}}}
    (f,) = hook.sub_findings({}, sync, set())
    assert f == {"kind": "subtiming", "lines": [{"code": "off", "track": "s2", "ref": ref, "why": "the cues are off", "offsets": [0.1, 2.1],
                                                 "unfixed": None}]}
    assert hook.texts(f, "done")[0].startswith(f"The subtitles (track 2) are out of sync compared with {says} by different amounts"), f


def test_a_failed_lookup_never_stops_apply_when_a_later_app_lists_every_path(env, monkeypatch, tmp_path):
    """Radarr times out, and Sonarr lists the episode. Every path has its item, so --apply goes on."""
    show = tmp_path / "tv" / "Show A"
    show.mkdir(parents=True)
    ep = show / "Show A - S01E02.mkv"
    shutil.copy(env["path"], ep)

    def fake_arr(app, p):
        if app == "radarr":
            raise TimeoutError("timed out")
        return {"series": [{"id": 5, "title": "Show A", "path": str(show)}], "episode?seriesId=5": [{"episodeFileId": 21, "seasonNumber": 1, "episodeNumber": 2, "runtime": 20}],
                "episodefile?seriesId=5": [{"id": 21, "path": str(ep), "seriesId": 5}]}[p]
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook, "PROFILES", {})
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    seen = []
    monkeypatch.setattr(hook, "process", lambda c: seen.append((c.app, c.path)) or {"outcome": "no_change", "result": "no change", "path": c.path, "label": "x"})
    hook.main(["--sub-time", str(ep), "--apply"])
    assert seen == [("sonarr", str(ep))], seen


def test_a_file_with_no_subtitle_gets_no_deep_analysis(env, monkeypatch, settings):
    settings(subtitles="deep")
    assert hook.queue_deep_analysis({"app": "radarr"}, {"path": env["path"], "tracks": [{"i": "a1"}]}, INPUTS) is None
    open(env["path"][:-4] + ".en.srt", "w").write(srt_text(REF))
    assert hook.queue_deep_analysis({"app": "radarr"}, {"path": env["path"], "tracks": [{"i": "a1"}]}, INPUTS)


def unlisted(env, monkeypatch):
    """No app lists the file. Radarr lists no movie, and Sonarr does not run on the host, which is no failed lookup."""
    def fake_arr(app, p):
        if app == "sonarr":
            raise FileNotFoundError("/var/lib/sonarr/config.xml")
        return []
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook.os, "nice", lambda n: None)


def test_sub_time_keeps_every_flag_of_a_file_no_app_lists(env, monkeypatch):
    """A copy of a Japanese film with English dub audio and an English subtitle on. No app lists it, so no original
    language is known. A decision without one would give the dub the default and turn the subtitle off. --apply
    keeps every flag."""
    japanese_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    unlisted(env, monkeypatch)
    assert hook.arr_decide.decide(env["probe"], None)["edits"]
    hook.main(["--sub-time", env["path"], "--apply"])
    rec = decided(env)
    assert env["mkvpropedit"] == [] and rec["flags_kept"].startswith("no app lists the file") and rec["outcome"] == "no_change", rec


@pytest.mark.parametrize("how", ["unlisted", "deep"])
def test_a_subtitle_verdict_still_turns_the_flags_off_when_the_other_flags_stay(env, monkeypatch, settings, how):
    """The English subtitle holds another film's lines, so it loses its default flag. With KEEP_ORIGINALS_DAYS 0 it
    stays in the file. That edit is the only one."""
    settings(keep_days=0, subtitles="deep")
    english_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    if how == "unlisted":
        unlisted(env, monkeypatch)
        hook.main(["--sub-time", env["path"], "--apply"])
    else:
        hook.deep_analysis(queue_analysis(env, env["path"]), [])
    rec = decided(env)
    assert rec["subcheck"]["s1"]["verdict"] == "mismatch" and rec["edits"] == [["track:=10", 0, 1]] and env["mkvpropedit"], rec.get("edits")


def test_a_subtitle_verdict_also_clears_the_forced_flag_when_the_other_flags_stay(env, monkeypatch, settings):
    """A full English subtitle flagged forced and default holds another film's lines. On a file no app lists it loses
    both flags, and nothing else changes."""
    settings(keep_days=0)
    english_film(env, ("eng", True, {"forced_track": True, "tag_number_of_frames": "400"}))
    env["probe"]["container"]["properties"]["writing_application"] = "mkvmerge v92.0"   # its statistics tags count
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    unlisted(env, monkeypatch)
    hook.main(["--sub-time", env["path"], "--apply"])
    rec = decided(env)
    assert rec["subcheck"]["s1"]["verdict"] == "mismatch" and rec["edits"] == [["track:=10", 0, 1], ["track:=10", 0, 1, "flag-forced"]], rec.get("edits")


@pytest.mark.parametrize("how", ["unlisted", "deep"])
def test_a_verdict_edit_keeps_its_result_when_the_decision_abstains(env, monkeypatch, settings, how):
    """English audio plays, and a second audio track is untagged, so a decision with no original language abstains.
    The English subtitle holds another film's lines, so it loses its default flag. The outcome stays "edited", and the
    reason stays in "undecided". The deep analysis asks Plex to analyze the file, and the cache holds nothing pending."""
    settings(keep_days=0, subtitles="deep")
    env["probe"] = sub_probe(("eng", True, {}), audio=(("eng", True), ("und", False)))
    env["movies"]["movie/7"]["runtime"] = round(talk.DURATION / 60)
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    cached, analyzed = [], []
    monkeypatch.setattr(hook, "sub_cache", lambda path, verdicts, pending: cached.append(pending))
    monkeypatch.setattr(hook, "plex_after", lambda app, source, rec, want: analyzed.append(rec["path"]) or {})
    if how == "unlisted":
        unlisted(env, monkeypatch)
        hook.main(["--sub-time", env["path"], "--apply"])
    else:
        hook.deep_analysis(queue_analysis(env, env["path"], original=None, want={"guids": ["tmdb://90001"]}), [])
    rec = decided(env)
    assert rec["outcome"] == "edited" and rec["undecided"] == "the original-language track may be the untagged one", (rec["outcome"], rec.get("undecided"))
    assert rec["edits"] == [["track:=10", 0, 1]] and cached == [False] and analyzed == ([env["path"]] if how == "deep" else []), (cached, analyzed)


def test_a_deep_remux_keeps_the_flags_of_its_import(env, monkeypatch, settings):
    """The deep analysis retimes the French track. A decision would now turn the English subtitle's default off, but
    the import set the flags, so they stay after the remux too."""
    settings(subtitles="deep")
    got = sub_time_film(env, monkeypatch, [("eng", True, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    assert hook.arr_decide.decide(env["probe"], "English")["edits"]
    hook.deep_analysis(queue_analysis(env, env["path"]), [])
    rec = decided(env)
    assert got and got[-1][0] and rec["subremux"]["done"] and env["mkvpropedit"] == [] and "flags_kept" in rec, (got, rec.get("result"))


def test_a_deep_analysis_never_hears_the_language_again(env, monkeypatch, settings):
    """The audio is untagged, so the import heard its language. The deep analysis decides with what the import stored."""
    settings(subtitles="deep")
    env["probe"] = sub_probe(("eng", False, {}), audio=(("und", True),))
    assert "audio_untagged_or_missing" in hook.arr_decide.decide(env["probe"], "English")["reasons"]
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    monkeypatch.setattr(hook, "hear", lambda *a, **k: pytest.fail("the deep analysis heard the language again"))
    hook.deep_analysis(queue_analysis(env, env["path"]), [])
    assert decided(env)["source"] == "deep_analysis"


def test_a_deep_analysis_stops_between_two_tracks_when_an_import_arrives(env, monkeypatch, settings):
    """Two tracks in time need no remux. An import arrives during the first fit, so the second track waits unread for
    the next run, and the job goes back to its queue."""
    settings(subtitles="deep")
    sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {}), ("spa", False, {})], {"s1": REF, "s2": REF, "s3": REF})
    real, fits = hook.arr_subsync.reference, []
    monkeypatch.setattr(hook.arr_subsync, "reference", lambda *a, **k: (fits.append(1) or enqueue(env, 1, env["path"])) and real(*a, **k))
    name = queue_analysis(env, env["path"])
    hook.deep_analysis(name, [])
    assert fits == [1] and [r["result"] for r in log_lines(env) if r.get("job") == name] == ["yielded"], fits


def test_a_deep_job_with_no_stored_inputs_is_dropped(env, monkeypatch, settings):
    """A job of an older version holds no decision inputs. It never runs, as it would decide with no original language."""
    settings(subtitles="deep")
    name = queue_analysis(env, env["path"])
    hook.put_job(name, {k: v for k, v in hook.job_of(name).items() if k != "inputs"})
    monkeypatch.setattr(hook, "process", lambda *a, **k: pytest.fail("a job with no inputs ran"))
    hook.deep_analysis(name, [])
    rec = decided(env)
    assert rec["result"] == "dropped, the job holds no decision inputs of its import" and hook.deep_analysis_queued() == [], rec


def test_a_yield_never_overwrites_a_newer_deep_job_of_the_path(env, monkeypatch, settings):
    """A newer import of the path queued its own job while the claimed one ran. The claimed one yields and goes, and
    the newer job keeps its inputs."""
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: (_ for _ in ()).throw(hook.Yielded("an import waits")))
    name = queue_analysis(env, env["path"], original="French")
    assert hook.claim(name)
    queue_analysis(env, env["path"], original="Spanish")
    hook.deep_analysis(name, [], True)
    assert hook.job_of(name)["inputs"]["original"] == "Spanish" and not claimed()


def test_sweep_offset_takes_the_median_of_the_windows_that_heard_enough():
    rows = [{"words": 15, "offset": x} for x in (0.4, 0.5, 0.6)] + [{"words": 2, "offset": -3.0}] * 4 + [{"words": 15, "offset": None}]
    assert hook.sweep_offset(rows) == 0.5 and hook.sweep_offset([]) == 0.0


def test_a_clean_sweep_reference_moves_into_audio_time_by_its_sweep(env, monkeypatch):
    """The English reference sits 0.6 s late, as its sweep heard. Windows under MIN_WORDS words say nothing. The French
    track sits 0.2 s early against the audio, so it keeps its times."""
    few = dict(IN_TIME, timing={"fix": None, "few": [100.0], "why": "under two windows hold 3 cues whose first words matched"})
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": talk.moved_to(REF, offset=0.6), "s2": talk.moved_to(REF, offset=-0.2)},
                        sync={"s1": few})
    rows = [{"at": 60.0 + 100 * k, "words": 15, "overlap": 0.9, "cues": 4, "offset": 0.6, "off": 0.0} for k in range(10)]
    rows += [{"at": 65.0 + 100 * k, "words": 2, "overlap": 0.0, "cues": 0, "offset": -3.0, "off": 0.0} for k in range(11)]
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({"s1": rows}, {"cpu": 0, "took": 0, "failed": []}))
    hook.main(["--sub-time", env["path"]])
    rec = decided(env)
    assert got == [] and rec["references"] == {"s1": "clean sweep"} and rec["subtime"]["s2"]["timing"]["why"] == "in time", rec["subtime"]["s2"]


def test_a_yielded_deep_analysis_goes_back_to_its_queue_unless_the_path_was_queued_again(env, monkeypatch, settings):
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: (_ for _ in ()).throw(hook.Yielded("an import waits")))
    name = queue_analysis(env, env["path"])
    assert hook.claim(name)   # a job process claimed it
    hook.deep_analysis(name, [], True)
    assert hook.deep_analysis_queued() == [name] and not claimed()
    assert hook.claim(name)
    queue_analysis(env, env["path"])   # a newer import of the path, while the claimed one runs
    hook.deep_analysis(name, [], True)
    assert hook.deep_analysis_queued() == [name] and not claimed()


def test_a_deep_analysis_left_claimed_by_a_dead_worker_goes_back_to_its_queue(env, monkeypatch, settings):
    settings(subtitles="deep")
    name = queue_analysis(env, env["path"])
    assert hook.claim(name)
    ran = []
    monkeypatch.setattr(hook, "deep_analysis", lambda n, pending, claimed=False: ran.append(n) or hook.drop_job(n, claimed))
    run_worker()
    assert ran == [name] and not claimed()
    # a newer import of the path queued its own job meanwhile: that job stays, and the claimed one goes
    queue_analysis(env, env["path"], original="French")
    assert hook.claim(name)
    queue_analysis(env, env["path"], original="Spanish")
    hook.requeue(name)
    assert hook.job_of(name)["inputs"]["original"] == "Spanish" and not claimed()


def test_the_deep_analysis_posts_only_its_subtitle_alerts(env, monkeypatch, settings):
    """The import alerted on the runtime. The deep analysis runs no metadata check and posts no other alert."""
    settings(subtitles="deep")
    english_film(env, ("eng", False, {}))
    env["movies"]["movie/7"]["runtime"] = 300   # the file runs far shorter than the app lists
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, where=lambda i: 8.0 if talk.FIRST + talk.GAP * i > talk.DURATION / 2 else 0.0)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    hook.main([])
    first = decided(env)
    assert "runtime" in first["alert_kinds"] and "subtiming" in first["alert_kinds"], first["alert_kinds"]
    rec = [r for r in log_lines(env) if r.get("outcome") and r["source"] == "deep_analysis"][-1]
    assert rec["alert_kinds"] == ["subtiming"] and "trusted" not in rec, rec["alert_kinds"]


@pytest.mark.parametrize("original", ["Japanese", None])
def test_a_deep_analysis_of_an_unchanged_file_keeps_the_flags_of_its_import(env, monkeypatch, settings, original):
    """A Japanese film: the Japanese audio plays, the English dub is off, and the English subtitle is on. The deep
    analysis asks no app and decides with the original language its import had. With none, the decision would turn the
    dub on and the subtitle off, but the subtitle verdict needs no new decision, so the flags stay."""
    settings(subtitles="deep")
    japanese_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    monkeypatch.setattr(hook, "arr", lambda app, p: pytest.fail(f"the deep analysis asked {app} for {p}"))
    assert hook.arr_decide.decide(env["probe"], "Japanese")["edits"] == [] and hook.arr_decide.decide(env["probe"], None)["edits"]
    hook.deep_analysis(queue_analysis(env, env["path"], original=original), [])
    rec = decided(env)
    assert rec["source"] == "deep_analysis" and rec["outcome"] == "no_change" and env["mkvpropedit"] == [], rec
    assert ("flags_kept" in rec) == (original is None) and rec["subcheck"]["s1"]["verdict"] == "match", rec


def test_a_hearing_leaves_no_onnxruntime_log_in_the_system_temp_dir(env, monkeypatch, tmp_path):
    """onnxruntime in the Whisper venv creates an empty log named by the process id on import. The hearing removes the
    one its own process left. A file of the same name from before the hearing, or one with content, stays."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(hook, "ORT_LOG", str(tmp / "mat-debug-{}.log"))
    old = tmp / "mat-debug-40.log"   # from an earlier process that had the pid of the first hearing
    old.write_text("")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    (tmp / "mat-debug-42.log").write_text("a line")

    class P:
        def __init__(self, argv, **k):
            self.pid = P.pid
            log = tmp / f"mat-debug-{self.pid}.log"
            if not log.exists():   # a log with content from onnxruntime's debug mode is left as it is
                log.write_text("")
        def communicate(self, timeout=None): return ('{"windows": []}', "")
        def poll(self): return 0
    for pid in (40, 42, 43):
        P.pid = pid
        monkeypatch.setattr(hook.subprocess, "Popen", P)
        hook.lid_cli(env["path"], 0, env["probe"], (), 60, False, words=("eng", [10.0], 10.0))
    assert sorted(os.listdir(tmp)) == ["mat-debug-40.log", "mat-debug-42.log"]   # the log of the hearing with pid 43 went


def test_the_report_after_a_removal_names_each_track_as_the_check_saw_it():
    """s1, an English track, left the file. The tracks after it moved up, so the report takes the places the check
    named. The alert names who removed it."""
    before = [{"i": "s1", "lang": "eng", "codec": "SubRip/SRT", "role": "full"}, {"i": "s2", "lang": "spa", "codec": "SubRip/SRT", "role": "full"}]
    rec = {"result": "no change", "label": "Film A", "path": "/m/Film A.mkv", "source": "backfill", "tracks": before[1:2] and [dict(before[1], i="s1")],
           "subcheck": {"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 5%", "timing": None},
                        "s2": {"verdict": "match", "why": "95%", "timing": {"fix": None, "why": "in time"}}},
           "subremux": {"done": True, "result": "subtitles remuxed", "remove": ["s1"], "removed": ["s1"], "kept": "/k/Film A.mkv", "tracks_before": before}}
    lines = hook.sub_time_report(rec, "done").splitlines()
    assert lines[2].startswith("  s1 | SubRip/SRT | eng | full") and "removed" in lines[2] and lines[3].startswith("  s2 | SubRip/SRT | spa"), lines
    sync = rec["subcheck"]
    assert [x["by"] for x in hook.sub_findings(rec, sync, set())[0]["lines"]] == ["run"]
    assert [x["by"] for x in hook.sub_findings(dict(rec, source="hook"), sync, set())[0]["lines"]] == ["hook"]


def test_swept_before_counts_the_sweep_of_the_pass_that_raised_replan():
    rec = {"sweep_facts": {"cpu": 0.0, "took": 0.2, "failed": [], "runs": 2, "cached": 2}}
    first = {"cpu": 170.5, "took": 172.0, "failed": ["audio 0: no words"], "runs": 2, "cached": 0}
    got = hook.swept_before(rec, hook.Replan("a subtitle needs a remux", first))["sweep_facts"]
    assert got == {"cpu": 170.5, "took": 172.2, "failed": ["audio 0: no words"], "runs": 4, "cached": 2}
    assert hook.swept_before({"sweep_facts": dict(rec["sweep_facts"])}, hook.Replan("the file changed"))["sweep_facts"] == rec["sweep_facts"]


def test_a_sub_time_apply_reports_the_sweep_of_both_passes(env, monkeypatch):
    """The apply hears the sweep, finds a fix, and plans again under the exclusive lock. The second pass finds the words
    in the cache. The sweep facts still count what the first pass heard."""
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    facts = iter([{"cpu": 170.5, "took": 172.0, "failed": [], "runs": 2, "cached": 0}, {"cpu": 0.0, "took": 0.2, "failed": [], "runs": 2, "cached": 2}])
    monkeypatch.setattr(hook, "sub_sweep", lambda path, j, items, sync, deep=False: ({"s1": sweep_rows([0.0] * 10)}, next(facts)))
    hook.main(["--sub-time", env["path"], "--apply"])
    f = decided(env)["sweep_facts"]   # 2 of 4 runs from the cache: the first pass heard its words
    assert got and f == {"cpu": 170.5, "took": 172.2, "failed": [], "runs": 4, "cached": 2}, f


def test_a_sweep_partly_from_the_cache_reports_its_cost():
    text = hook.sub_time_report({"result": "no change", "label": "f", "path": "/m/f.mkv", "sweep_facts": {"cpu": 30.5, "took": 31.0, "failed": [], "runs": 2,
                                                                                                           "cached": 1}}, "done")
    assert "sweep cost: 30.5 CPU s, 31.0 s" in text and "words from the cache" not in text


@pytest.mark.parametrize("rec, why", [
    ({"outcome": "error", "result": "error: OSError: gone"}, "error: OSError: gone"),
    ({"outcome": "verify_failed", "result": "VERIFY FAILED, flags did not change"}, "VERIFY FAILED, flags did not change"),
    ({"outcome": "edited", "result": "edited", "sidecars": [{"name": "f.fr.srt", "result": "left"}, {"name": "f.es.srt", "result": "retimed"}]},
     "the sidecar f.fr.srt stays as it was"),
    ({"outcome": "no_change", "result": "no change", "header_repair": {
        "result": "header repair failed: the original could not be kept, so it stays: too little for a copy", "code": "header_repair_failed"}},
     "header repair failed: the original could not be kept, so it stays: too little for a copy"),
    ({"outcome": "no_change", "result": "no change", "header_repair": {"result": "header repair skipped, hardlinked: x", "code": "header_repair_skipped"}},
     "header repair skipped, hardlinked: x"),
    ({"outcome": "edited", "result": "edited", "subremux": {"fixed": ["s2"], "done": True},
      "header_repair": {"result": "header repaired", "code": "header_repaired"}}, None)])
def test_missed_names_each_change_that_did_not_happen(rec, why):
    assert hook.missed(rec) == why


def test_a_verdict_edit_keeps_its_result_when_the_decision_drops(env, monkeypatch, settings):
    """A decision that drops its plan, as when a rule it needs failed, has no edits of its own. The subtitle verdict
    still turns the flags off, and the outcome stays "edited" with the reason in "dropped"."""
    settings(keep_days=0)
    english_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    monkeypatch.setattr(hook, "sub_sweep", lambda *a, **k: ({}, {"cpu": 0, "took": 0, "failed": []}))
    real = hook.arr_decide.decide
    monkeypatch.setattr(hook.arr_decide, "decide", lambda *a, **k: dict(real(*a, **k), edits=[], dropped=["a rule it needs did not load"]))
    unlisted(env, monkeypatch)
    hook.main(["--sub-time", env["path"], "--apply"])
    rec = decided(env)
    assert rec["outcome"] == "edited" and rec["dropped"] == ["a rule it needs did not load"] and rec["edits"] == [["track:=10", 0, 1]], rec["outcome"]


def test_keepable_needs_room_for_a_copy_only_on_another_file_system(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    path = tmp_path / "f.mkv"
    path.write_bytes(b"x" * 1000)
    st = os.stat(path)
    other = types.SimpleNamespace(st_dev=st.st_dev + 1, st_size=1000)
    monkeypatch.setattr(hook.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=1000 + hook.KEEP_COPY_FREE))
    assert hook.keepable(str(path), st) is None and hook.keepable(str(path), other) is None
    monkeypatch.setattr(hook.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=1000 + hook.KEEP_COPY_FREE - 1))
    assert hook.keepable(str(path), st) is None   # a hard link needs no room
    assert hook.keepable(str(path), other).startswith(f"{tmp_path} is on another file system with 1.1 GB free, too little for a copy")


def test_a_copy_of_the_same_size_with_other_bytes_is_never_kept(sync_mkv, tmp_path, monkeypatch):
    """A write went wrong in place, so the copy has the size of the original and other bytes. The hash sees it."""
    path, before, st = refused_link(sync_mkv, tmp_path, monkeypatch)
    real = os.fsync

    def fsync(fd):
        if os.readlink(f"/proc/self/fd/{fd}").endswith(".copying"):
            os.pwrite(fd, b"y", 100)
        return real(fd)
    monkeypatch.setattr(hook.os, "fsync", fsync)
    _, result, info = hook.resub(path, REAL_MKVMERGE(path), st, True, {2: {"rate": "1/1", "offset": 1.0}})
    assert "the copy of the original does not match it" in result and open(path, "rb").read() == before and kept_files(tmp_path) == [], result


def test_a_damage_work_folder_that_a_killed_step_left_goes_when_a_worker_starts(env):
    folder = hook.work_dir("damage")
    assert os.path.dirname(folder) == hook.CFG.state_dir and os.path.basename(folder).startswith(".damage-")
    os.utime(folder, (time.time() - 86400 - 60, time.time() - 86400 - 60))
    hook.stale_work_dirs()
    assert not os.path.exists(folder)


def test_lid_cli_passes_the_sweep_group_and_where_to_yield(env, monkeypatch):
    seen = []

    class P:
        pid = 1
        def __init__(self, argv, **k): seen.append(argv)
        def communicate(self, timeout=None): return ('{"windows": []}', "")
        def poll(self): return 0
    monkeypatch.setattr(hook.subprocess, "Popen", P)
    hook.lid_cli(env["path"], 0, env["probe"], (), 60, False, words=("eng", [10.0, 70.0], 10.0, None, 2), yield_to=("/s/lid.turn.gate", "/s/queue"))
    argv = seen[0]
    assert argv[argv.index("--group") + 1] == "2" and argv[argv.index("--yield-gate") + 1] == "/s/lid.turn.gate" and argv[-2:] == ["--yield-queue", "/s/queue"]


def test_job_processes_run_one_deep_analysis_at_a_time(pool, monkeypatch, settings):
    settings(subtitles="deep")
    a, b = films(pool, 2)
    queue_analysis(pool, a, owner="1")
    queue_analysis(pool, b, owner="2")
    real = hook.process

    def process(ctx):
        trace("deep start", path=ctx.path); real_wait(0.3)
        try:
            return real(ctx)
        finally:
            trace("deep end", path=ctx.path)
    monkeypatch.setattr(hook, "process", process)
    run_worker()
    rows = [r["what"] for r in traced() if r["what"] in ("deep start", "deep end")]
    assert rows == ["deep start", "deep end"] * 2 and len({r["pid"] for r in traced("deep start")}) == 2, traced()
    assert hook.deep_analysis_queued() == [] and not claimed()


@pytest.mark.parametrize("cached", [True, False])
def test_the_sweep_report_says_when_its_words_came_from_the_cache(env, monkeypatch, cached):
    """An apply after a dry run finds the sweep's words in the cache, so it heard nothing and cost no CPU time."""
    english_film(env, ("eng", False, {}))
    items = {"s1": ("eng", 0, talk.cues(talk.RIGHT))}
    monkeypatch.setattr(hook, "lid_run", lambda path, index, j, expect, timeout, words=None, yield_to=None, **k:
                        {"windows": [{"at": a, "words": []} for a in words[1]], "cached": cached, "cpu": 0.0 if cached else 30.5, "took": 0.2})
    rows, facts = hook.sub_sweep(env["path"], env["probe"], items, {})
    text = hook.sub_time_report({"result": "no change", "label": "Film A", "path": env["path"], "sweep": rows, "sweep_facts": facts}, "done")
    assert ("  sweep: words from the cache" in text) == cached and ("sweep cost: 30.5 CPU s" in text) != cached, text[-200:]


@pytest.mark.parametrize("code, result", [("subtitle_remux_failed", "subtitle remux failed: the original could not be kept, so it stays"),
                                          ("subtitle_remux_skipped", "subtitle remux skipped, low space: 1.0 GB free")])
def test_sub_time_apply_exits_3_when_a_planned_change_did_not_happen(env, monkeypatch, capsys, code, result):
    """The French track needs a fix of 2 s, and the remux fails. The dry run of the same file exits 0."""
    sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": REF, "s2": talk.moved_to(REF, offset=2.0)})
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), ends=None:
                        (code, result, {}) if apply else ("would_remux_subtitles", "would remux subtitles: x", {}))
    hook.main(["--sub-time", env["path"]])
    with pytest.raises(SystemExit) as ex:
        hook.main(["--sub-time", env["path"], "--apply"])
    assert ex.value.code == hook.SUB_TIME_MISSED == 3 and f"{env['path']}: {result}" in capsys.readouterr().out


def test_a_sweep_that_yields_to_a_waiting_hearing_hears_the_rest_with_its_next_turn(env, monkeypatch):
    """Another hearing waited at the gate, so the sweep stopped after one pair. --sub-time takes the turn again and
    hears the rest. It never yields to the job queue, which only the deep analysis leaves for."""
    english_film(env, ("eng", False, {}))
    items = {"s1": ("eng", 0, talk.cues(talk.RIGHT))}
    calls = []

    def lid_run(path, index, j, expect, timeout, words=None, yield_to=None, **k):
        calls.append((list(words[1]), yield_to))
        ws = words[1][:2] if len(calls) == 1 else words[1]
        return dict({"windows": [{"at": a, "words": []} for a in ws], "cpu": 1.0, "took": 1.0}, **({"yielded": True} if len(calls) == 1 else {}))
    monkeypatch.setattr(hook, "lid_run", lid_run)
    enqueue(env, 1, env["path"])   # an import waits in the queue, which --sub-time does not wait for
    rows, facts = hook.sub_sweep(env["path"], env["probe"], items, {})
    assert len(calls) == 2 and calls[1][0] == calls[0][0][2:] and calls[0][1][1] is None and len(rows["s1"]) == len(calls[0][0]), calls


def test_a_target_is_judged_against_the_audio_and_not_the_reference_offset(env, monkeypatch):
    """The English reference is in time but sits 0.6 s late, its measured offset. The French track sits 0.2 s early
    against the audio. Against the audio it is in time, so it keeps its times. Against the reference it would get a
    fix of -0.8 s and end 0.6 s late."""
    early = talk.moved_to(REF, offset=-0.2)
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": talk.moved_to(REF, offset=0.6), "s2": early},
                        sync={"s1": dict(IN_TIME, timing={"fix": None, "why": "in time", "offset": 0.6})})
    hook.main(["--sub-time", env["path"]])
    r = decided(env)["subtime"]["s2"]
    assert got == [] and r["timing"]["why"] == "in time", r


@pytest.mark.parametrize("seed", range(6))
def test_right_targets_beside_an_offset_reference_keep_their_times(env, monkeypatch, seed):
    """Right tracks jitter up to 0.3 s against the audio, and the reference sits up to 0.5 s off it, both ways."""
    rnd = random.Random(seed)
    off = rnd.choice((-0.5, -0.3, 0.3, 0.5))
    target = [(a + rnd.uniform(-0.3, 0.3), b, x) for a, b, x in REF]
    got = sub_time_film(env, monkeypatch, [("eng", False, {}), ("fre", False, {})], {"s1": talk.moved_to(REF, offset=off), "s2": target},
                        sync={"s1": dict(IN_TIME, timing={"fix": None, "why": "in time", "offset": off})})
    hook.main(["--sub-time", env["path"]])
    assert got == [] and decided(env)["subtime"]["s2"]["timing"]["why"] == "in time", decided(env)["subtime"]["s2"]


# --- the state store (docs/design.md, "State") ----------------------------------------------------------------------

def test_a_busy_store_never_slows_or_fails_an_import(env, monkeypatch, settings):
    """The whole hook run waits HOOK_WAIT for the store at most: the claims of an upgrade's three old files, the job,
    and the look for waiting work, with six job files of earlier runs in STATE_DIR/queue. Then its job goes there as a
    file too, a worker starts for them, and the worker moves them into the queue of the store."""
    settings(keep_replaced=True, keep_days=7)
    monkeypatch.setattr(hook.store, "HOOK_WAIT", 0.5)
    monkeypatch.setattr(hook.store, "fork", lambda: 4242)   # the other writer below is a connection of this process
    monkeypatch.setenv("radarr_deletedpaths", "|".join(f"/m/Film A (1979)/old{n}.mkv" for n in range(3)))
    monkeypatch.setenv("radarr_deletedrecyclebinpaths", "||")
    hook.store.db()
    os.makedirs(hook.queue_dir())
    for n in range(6):
        with open(os.path.join(hook.queue_dir(), f"{n + 1}-1.json"), "w") as f:
            json.dump({"app": "radarr", "path": f"/m/{n}.mkv", "time": env["clock"][0]}, f)
    with contextlib.closing(sqlite3.connect(hook.store.path(), isolation_level=None, check_same_thread=False)) as other:
        other.execute("BEGIN IMMEDIATE")   # another process writes, and holds the store for 3 seconds
        later = threading.Timer(3.0, other.execute, ("ROLLBACK",))
        later.start()
        started = time.perf_counter()
        with pytest.raises(SystemExit):
            hook.main([])
        took = time.perf_counter() - started
        later.join()
    assert took < 1.5 and env["exit"] == [0], took   # a fork for the worker, and the app got its answer
    (f,) = set(os.listdir(hook.queue_dir())) - {f"{n + 1}-1.json" for n in range(6)}
    assert hook.queued() == sorted([f] + [f"{n + 1}-1.json" for n in range(6)]) and os.listdir(hook.queue_dir()) == []
    assert hook.job_of(f)["path"] == env["path"]


def test_the_look_for_waiting_work_never_waits_for_a_busy_store(env, monkeypatch):
    """The listener's loop and the hook look for waiting work with the store busy and job files in STATE_DIR/queue. The
    look only reads, so it answers at once, and the files wait for the worker."""
    monkeypatch.setattr(hook.store, "wait", 0.5)   # the listener's wait
    hook.store.db()
    os.makedirs(hook.queue_dir())
    for n in range(6):
        open(os.path.join(hook.queue_dir(), f"{n + 1}-1.json"), "w").close()
    with contextlib.closing(sqlite3.connect(hook.store.path(), isolation_level=None)) as other:
        other.execute("BEGIN IMMEDIATE")
        started = time.perf_counter()
        assert hook.waiting()
        assert time.perf_counter() - started < 0.3 and len(os.listdir(hook.queue_dir())) == 6
        other.execute("ROLLBACK")


def test_parallel_claims_take_each_job_once(env):
    """Six coordinators claim the same twenty jobs. Each job goes to one of them."""
    names = [enqueue(env, n, env["path"]) for n in range(20)]
    pids = []
    for i in range(6):
        pid = real_fork()
        if not pid:
            try:
                with open(os.path.join(hook.CFG.state_dir, f"got{i}"), "w") as f:
                    json.dump([n for n in names if hook.claim(n)], f)
            finally:
                REAL_EXIT(0)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    got = [n for i in range(6) for n in json.load(open(os.path.join(hook.CFG.state_dir, f"got{i}")))]
    assert sorted(got) == sorted(names) == claimed() and hook.queued() == []


@pytest.mark.parametrize("ns, hours, stranded", [("pid:[1]", 23, False), ("pid:[1]", 25, True), ("pid:[1]", None, True), (None, 1, True),
                                                  ("here", 1, True)])
def test_a_pending_conversion_of_another_pid_namespace_counts_as_alive_for_a_day(env, monkeypatch, ns, hours, stranded):
    """G5: a pid of another pid namespace, as of a container beside the host, means nothing here. Its conversion may
    still run, so nothing reports it for JOB_MAX_AGE. After that it counts as stopped, as after a restart that gave the
    container a new namespace, and so does an entry with no time. An entry with no namespace counts as one of this
    namespace."""
    dead = subprocess.Popen(["true"]); dead.wait()
    stamp = hours and datetime.datetime.fromtimestamp(env["clock"][0] - hours * 3600).astimezone().isoformat(timespec="seconds")   # None: no time
    entry = dict(app="radarr", owner=7, items=[7], path="/m/A/A.mp4", new="/m/A/A.mkv", held="/m/A/.held", old_id=11, pid=dead.pid, start=None,
                 time=stamp, state="placed", extras=[], **({"pid_ns": hook.pid_ns() if ns == "here" else ns} if ns else {}))
    hook.pending_edit("radarr:1:ab12cd34", entry)
    # a live process here that holds the same pid number never keeps an entry of another namespace hidden
    monkeypatch.setattr(hook, "job_alive_pid", lambda pid, start=None: ns == "pid:[1]")
    assert list(hook.pending_recover("radarr", False)) == (["radarr:1:ab12cd34"] if stranded else [])
    assert hook.pid_ns() == os.readlink("/proc/self/ns/pid") != "pid:[1]"


def test_an_editing_line_with_no_edited_line_is_a_failed_edit_for_two_weeks(env):
    """The store keeps the mark of an editing line until an edited line of the path. A mark older than KEEP_DECISIONS
    goes."""
    path = env["path"]
    hook.log(dict(source="hook", path=path, result="editing", undo="mkvpropedit ..."))
    assert hook.hook_edit_failed(path) and not hook.hook_edit_failed(path + ".other")
    hook.log(dict(source="hook", path=path, result="edited"))
    assert not hook.hook_edit_failed(path)
    hook.log(dict(source="hook", path=path, result="editing"))
    hook.log(dict(source="hook", path=path, result="dropped", edit_result="edited"))
    assert not hook.hook_edit_failed(path)
    hook.log_facts(dict(path=path, result="editing"), time.time() - hook.store.KEEP_DECISIONS - 60)
    hook.decision(dict(app="radarr", source="hook", path="/m/x.mkv", result="no change", outcome="no_change"), time.time())
    assert not hook.hook_edit_failed(path)


def test_a_store_error_on_a_log_line_loses_the_fact_and_never_the_job(env, monkeypatch):
    """The callers of log() expect an OSError at most. A busy store loses the editing mark or the analyze time, and the
    line is in the log."""
    hook.store.db()
    monkeypatch.setattr(hook.store, "put", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    hook.log(dict(source="hook", path=env["path"], result="editing"))
    hook.log(dict(ANALYZE_LINE))
    assert [r["result"] for r in log_lines(env)] == ["editing", "plex"]


def test_the_latest_analyze_counts_when_two_processes_log_out_of_order(env):
    hook.log_facts(ANALYZE_LINE, 2000.0)
    hook.log_facts(ANALYZE_LINE, 1000.0)   # another process wrote its older line later
    assert hook.store.get("plex-analyzed", "12") == 2000.0


def test_the_kept_records_stat_the_links_before_the_transaction(env, monkeypatch, settings):
    """A link on a NAS that hangs never holds the store. A record older than keep_days stays while its link exists."""
    settings(keep_days=7)
    old = env["clock"][0] - 8 * 86400
    hook.store.put("kept", "replaced", [dict(app="radarr", old="/m/a.mkv", kept=env["path"], download_id="", time=old, ino=1),
                                         dict(app="radarr", old="/m/b.mkv", kept="/nonexistent/b.mkv", download_id="", time=old, ino=2)])
    stats = []
    real = hook.os.path.lexists
    monkeypatch.setattr(hook.os.path, "lexists", lambda p: stats.append(hook.store.db().in_transaction) or real(p))
    with hook.kept_records() as recs:
        assert [r["old"] for r in recs] == ["/m/a.mkv"]
    assert stats == [False, False]


def test_the_audit_reads_the_decisions_of_its_window_from_the_store(env, capsys):
    """--since reads the decision lines of its app from that time on. The store drops a line after KEEP_DECISIONS."""
    hook.main([])   # an edit of Film A, logged now
    old = dict(audit_line("Show H", 1, ENGLISH_FIRST), app="radarr", time="2026-01-01T00:00:00+00:00")
    hook.store.decided(datetime.datetime.fromisoformat(old["time"]).timestamp(), "radarr", old["path"], json.dumps(old))
    hook.main(["--audit", "radarr", "--since", "2026-01-02"])
    assert "1 files edited since" in capsys.readouterr().out
    hook.main(["--audit", "radarr", "--since", "2025-12-31"])
    assert "2 files edited since" in capsys.readouterr().out
    hook.main(["--audit", "sonarr", "--since", "2025-12-31"])
    assert "0 files edited since" in capsys.readouterr().out
    hook.decision(dict(app="radarr", source="hook", path="/m/y.mkv", result="no change", outcome="no_change"), time.time())
    assert [json.loads(r)["path"] for r, in hook.store.read("SELECT rec FROM decisions")] == [env["path"], "/m/y.mkv"]   # the old line went


STORE_WRITER = """
import contextlib, sqlite3, sys
with contextlib.closing(sqlite3.connect(sys.argv[1], isolation_level=None)) as db:
    if sys.argv[2:]:
        db.execute("INSERT INTO kv VALUES ('t', ?, 'null', 0)", (sys.argv[2],))
    print(" ".join(k for k, in db.execute("SELECT key FROM kv WHERE ns = 't' ORDER BY key")))
"""


def test_a_new_thread_keeps_the_store_locks_of_its_process(env):
    """SQLite holds POSIX locks on the store file, and a close of any descriptor of that file drops every lock of the
    process. Then the last connection of another process takes the store as its own, writes the WAL back and deletes
    it, and this process writes on into the deleted WAL. A new thread's first db() opens no descriptor of the file."""
    hook.store.put("t", "main", None)   # the connection of the listener's main loop
    t = threading.Thread(target=hook.store.put, args=("t", "thread", None))   # a request thread of the listener
    t.start(); t.join()
    REAL_RUN([sys.executable, "-c", STORE_WRITER, hook.store.path(), "other"], check=True, capture_output=True)   # a worker
    hook.store.put("t", "after", None)
    out = REAL_RUN([sys.executable, "-c", STORE_WRITER, hook.store.path()], check=True, capture_output=True, text=True).stdout
    assert out.split() == ["after", "main", "other", "thread"], out


def test_a_thread_keeps_its_store_connection_until_the_store_file_goes(env):
    """db() gives each thread one connection. A store file that was removed gets a new connection and a new store."""
    c = hook.store.db()
    hook.store.put("t", "a", 1)
    assert hook.store.db() is c
    for n in os.listdir(hook.CFG.state_dir):
        if n.startswith(hook.store.FILE):
            os.remove(os.path.join(hook.CFG.state_dir, n))
    assert hook.store.get("t", "a") is None and hook.store.db() is not c and hook.store.read("PRAGMA auto_vacuum") == [(2,)]


def test_the_wal_file_goes_back_to_4_mb_after_a_large_write(env):
    """SQLite keeps the -wal file at its peak size. journal_size_limit cuts it to 4 MB once a checkpoint resets it."""
    with hook.store.tx():
        for i in range(12):
            hook.store.put("big", str(i), "x" * (1 << 20))
    hook.store.drop("big")
    hook.store.put("t", "a", 1)
    assert os.path.getsize(hook.store.path() + "-wal") <= 4 << 20


def test_the_nightly_audit_gives_the_space_of_old_rows_back_to_the_disk(env, capsys):
    """A deleted row leaves a free page, and SQLite never shrinks the file by itself. A new decision line deletes the
    rows older than KEEP_DECISIONS, and the nightly audit frees their pages, see store.shrink()."""
    old = env["clock"][0] - hook.store.KEEP_DECISIONS - 60
    with hook.store.tx() as c:   # two weeks of backfill rows
        c.executemany("INSERT INTO decisions VALUES (?, 'radarr', ?, ?)", [(old, f"/m/{i}.mkv", "x" * 2000) for i in range(2000)])
    on_disk = lambda: hook.store.db().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall() and os.path.getsize(hook.store.path())
    full = on_disk()
    hook.decision(dict(app="radarr", source="hook", path="/m/x.mkv", result="no change", outcome="no_change"), time.time())
    assert hook.store.read("SELECT COUNT(*) FROM decisions") == [(1,)] and on_disk() >= full   # the rows went, and the file kept its size
    hook.main(["--audit", "radarr", "--since", "24h"])
    assert on_disk() < full / 10, (full, on_disk())
