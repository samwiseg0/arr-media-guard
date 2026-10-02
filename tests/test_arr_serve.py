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
"""Unit tests for serve.py, the Webhook listener, and for PATH_MAP and the API key.

No network beyond 127.0.0.1. The package is loaded as h, see amg.py, and h.arr_serve is its listener. The app API is a fake. The HTTP tests run the real handler on a free local port. One test forks
the real listener and stops it with SIGTERM.

Run: pytest tests/test_arr_serve.py
"""
import base64
import contextlib
import dataclasses
import datetime
import hashlib
import http.client
import json
import os
import re
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import syslog
import threading
import time
import urllib.error
import urllib.parse

import pytest

import amg

FILES = os.path.join(os.path.dirname(__file__), "..")
os.environ["ARR_MEDIA_GUARD_LIB"] = FILES
os.environ["ARR_MEDIA_GUARD_ENV"] = "/nonexistent/arr-media-guard.env"
h = amg.load("arr_media_guard_serve")
arr_serve = h.arr_serve

with open(os.path.join(FILES, "examples", "policy.json")) as _f:
    h.arr_decide.set_policy(json.load(_f))
AUTH = "Basic " + base64.b64encode(b"guard:s3cret-pass").decode()


def http_error(code):
    return urllib.error.HTTPError("http://app.invalid", code, "error", {}, None)


@pytest.fixture(autouse=True)
def store_waits(monkeypatch):
    """hook() and the listener bound the waits for the store of their process, and each test starts with the usual ones."""
    monkeypatch.setattr(h.store, "wait", h.store.WAIT)
    monkeypatch.setattr(h.store, "until", None)


@pytest.fixture
def app(tmp_path, monkeypatch, settings):
    """A fake Radarr and Sonarr with one film and one episode file on disk, an upgrade's old files and a recycle bin.
    api maps an API path to its answer, or to an exception it raises. Returns the fake's state."""
    movies, tv, rbin = tmp_path / "movies" / "Film A (1979)", tmp_path / "tv" / "Show A" / "Season 1", tmp_path / "recycle"
    for d in (movies, tv, rbin):
        d.mkdir(parents=True)
    film, ep = movies / "Film A (1979) WEBDL-1080p.mkv", tv / "Show A - S01E02 - Two WEBDL-1080p.mkv"
    film.write_bytes(b"x")
    ep.write_bytes(b"x")
    state = tmp_path / "state"
    state.mkdir()
    settings(state_dir=str(state), log=str(tmp_path / "log.jsonl"), path_map=[])
    monkeypatch.setattr(arr_serve, "REFUSALS", arr_serve.Refusals())
    s = {"film": str(film), "ep": str(ep), "movies": str(movies), "tv": str(tv.parent), "rbin": str(rbin), "calls": [], "api": {
        "moviefile/31": {"id": 31, "movieId": 7, "path": str(film), "sceneName": "Film.A.1979.1080p.WEB-DL-GRP"},
        "episodefile/41": {"id": 41, "seriesId": 5, "path": str(ep), "sceneName": None},
        "episode?episodeFileId=41": [{"id": 902}, {"id": 901}],
        "movie/7": {"id": 7, "path": str(movies)}, "series/5": {"id": 5, "path": str(tv.parent)},
        "config/mediamanagement": {"recycleBin": str(rbin)},
        "rootfolder": [{"path": str(tmp_path / "movies")}, {"path": str(tmp_path / "tv")}],
    }}

    def fake_arr(a, p):
        s["calls"].append((a, p))
        v = s["api"][p]
        if isinstance(v, Exception):
            raise v
        return h.mapped(v, a)   # the real arr() maps every answer with the app's map
    monkeypatch.setattr(h, "arr", fake_arr)
    return s


def radarr_body(s, **kw):
    body = {"eventType": "Download", "instanceName": "Radarr", "movie": {"id": 7, "title": "Film A", "folderPath": s["movies"]},
            "movieFile": {"id": 31, "path": s["film"], "sceneName": "Film.A.1979.1080p.WEB-DL-GRP"}, "isUpgrade": False,
            "downloadId": "SABnzbd_nzo_abc123"}
    return dict(body, **kw)


def sonarr_body(s, **kw):
    body = {"eventType": "Download", "instanceName": "Sonarr", "series": {"id": 5, "title": "Show A", "path": s["tv"]},
            "episodes": [{"id": 901}, {"id": 902}], "episodeFile": {"id": 41, "path": s["ep"], "sceneName": None},
            "isUpgrade": False, "downloadId": None}
    return dict(body, **kw)


def hook_job(monkeypatch, env):
    """The job hook() writes for the Custom Script variables env. No worker starts."""
    for k in list(os.environ):
        if k.startswith(("radarr_", "sonarr_")):
            monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(h, "try_lock", lambda name: None)   # a worker runs, so hook() only queues
    h.hook()
    (name,) = h.queued()
    job = h.job_of(name)
    h.drop_job(name)
    return job


def log_lines():
    with open(h.CFG.log) as f:
        return [json.loads(line) for line in f]


def env_event(monkeypatch, env):
    """The Event hook() reads from the Custom Script variables env, with a time of 0."""
    for k in list(os.environ):
        if k.startswith(("radarr_", "sonarr_")):
            monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return dataclasses.replace(h.Event.from_env("radarr" if "radarr_eventtype" in env else "sonarr"), time=0)


# --- the Webhook body gives the job of the Custom Script variables -------------------------------------------------

def test_a_radarr_upgrade_body_gives_the_job_hook_writes(app, monkeypatch):
    old, copy = os.path.join(app["movies"], "Film A (1979) HDTV-720p.mkv"), os.path.join(app["rbin"], "Film A (1979)", "Film A (1979) HDTV-720p.mkv")
    old2 = os.path.join(app["movies"], "Film A (1979) HDTV-720p.en.srt")
    want = hook_job(monkeypatch, {"radarr_eventtype": "Download", "radarr_movie_id": "7", "radarr_moviefile_id": "31",
                                  "radarr_moviefile_path": app["film"], "radarr_moviefile_scenename": "Film.A.1979.1080p.WEB-DL-GRP",
                                  "radarr_download_id": "SABnzbd_nzo_abc123", "radarr_deletedpaths": f"{old}|{old2}",
                                  "radarr_deletedrecyclebinpaths": f"{copy}|"})
    got = arr_serve.download("radarr", radarr_body(app, isUpgrade=True, deletedFiles=[
        {"id": 30, "path": old, "recycleBinPath": copy}, {"id": 29, "path": old2, "recycleBinPath": None}]))
    assert {k: v for k, v in got.items() if k != "time"} == {k: v for k, v in want.items() if k != "time"}
    assert list(got) == list(want)


def test_a_sonarr_body_gives_the_job_hook_writes_with_the_apis_episode_ids(app, monkeypatch):
    want = hook_job(monkeypatch, {"sonarr_eventtype": "Download", "sonarr_series_id": "5", "sonarr_episodefile_id": "41",
                                  "sonarr_episodefile_path": app["ep"], "sonarr_episodefile_episodeids": "901,902",
                                  "sonarr_episodefile_scenename": "", "sonarr_download_id": ""})
    got = arr_serve.download("sonarr", sonarr_body(app, episodes=[{"id": 1}]))   # the posted episodes are never used
    assert {k: v for k, v in got.items() if k != "time"} == {k: v for k, v in want.items() if k != "time"}
    assert got["episode_ids"] == "901,902" and got["deleted"] is None and got["recycled"] is None


def test_a_sonarr_body_reads_the_nfo_beside_its_source_as_the_hook_does(app, monkeypatch, tmp_path):
    """Both read the scene NFO of the folder Sonarr imported from, at the event."""
    rel = tmp_path / "downloads" / "Show.A.S01E02.1080p.WEB.H264-GRP"
    rel.mkdir(parents=True)
    (rel / "show.a.s01e02.1080p.web.h264-grp.nfo").write_text("Title : Night Shift\n")
    src = str(rel / "show.a.s01e02.1080p.web.h264-grp.mkv")
    want = hook_job(monkeypatch, {"sonarr_eventtype": "Download", "sonarr_series_id": "5", "sonarr_episodefile_id": "41",
                                  "sonarr_episodefile_path": app["ep"], "sonarr_episodefile_episodeids": "901,902",
                                  "sonarr_episodefile_scenename": "", "sonarr_download_id": "", "sonarr_episodefile_sourcepath": src})
    got = arr_serve.download("sonarr", sonarr_body(app, episodeFile={"id": 41, "path": app["ep"], "sourcePath": src}))
    assert got["nfo_title"] == want["nfo_title"] == "Night Shift"
    assert arr_serve.download("sonarr", sonarr_body(app, episodeFile={"id": 41, "path": app["ep"], "sourcePath": "../x.mkv"}))["nfo_title"] is None


def test_the_hook_and_the_listener_build_the_same_event(app, monkeypatch, settings):
    """Download, Grab and Test give one Event from the Custom Script variables and from the Webhook body. Sonarr names
    the episodes of a grab by number in the variables and by id in the body."""
    hook_of = lambda app_, body: dataclasses.replace(h.Event.from_webhook(app_, body), time=0)
    old = os.path.join(app["movies"], "Film A (1979) HDTV-720p.mkv")
    copy = os.path.join(app["rbin"], "Film A (1979)", "Film A (1979) HDTV-720p.mkv")
    got = env_event(monkeypatch, {"radarr_eventtype": "Download", "radarr_movie_id": "7", "radarr_moviefile_id": "31",
                                  "radarr_moviefile_path": app["film"], "radarr_moviefile_scenename": "Film.A.1979.1080p.WEB-DL-GRP",
                                  "radarr_download_id": "SABnzbd_nzo_abc123", "radarr_deletedpaths": old, "radarr_deletedrecyclebinpaths": copy})
    assert got == hook_of("radarr", radarr_body(app, isUpgrade=True, deletedFiles=[{"id": 30, "path": old, "recycleBinPath": copy}]))
    assert got.path == app["film"] and got.deleted == old
    got = env_event(monkeypatch, {"sonarr_eventtype": "Download", "sonarr_series_id": "5", "sonarr_episodefile_id": "41",
                                  "sonarr_episodefile_path": app["ep"], "sonarr_episodefile_episodeids": "901,902",
                                  "sonarr_episodefile_scenename": "", "sonarr_download_id": ""})
    assert got == hook_of("sonarr", sonarr_body(app)) and got.episode_ids == "901,902"
    app["api"]["episode?seriesId=5&seasonNumber=1"] = [{"id": 903, "episodeNumber": 3}, {"id": 902, "episodeNumber": 2}, {"id": 901, "episodeNumber": 1}]
    got = env_event(monkeypatch, {"sonarr_eventtype": "Grab", "sonarr_series_id": "5", "sonarr_release_seasonnumber": "1",
                                  "sonarr_release_episodenumbers": "2,1", "sonarr_download_id": "D1"})
    assert got == hook_of("sonarr", {"eventType": "Grab", "series": {"id": 5}, "episodes": [{"id": 902}, {"id": 901}], "downloadId": "D1"})
    assert (got.owner, got.eps, got.download_id) == ("5", [901, 902], "D1")
    got = env_event(monkeypatch, {"radarr_eventtype": "Grab", "radarr_movie_id": "7"})
    assert got == hook_of("radarr", {"eventType": "Grab", "movie": {"id": 7}}) and (got.owner, got.eps, got.download_id) == ("7", [], "")
    for name in ("radarr", "sonarr"):
        got = env_event(monkeypatch, {f"{name}_eventtype": "Test"})
        assert got == hook_of(name, {"eventType": "Test"}) == h.Event(name, "Test", 0)


def test_the_job_takes_the_apis_path_and_scene_name_and_logs_the_posted_path(app):
    other = os.path.join(app["movies"], "elsewhere.mkv")
    job = arr_serve.download("radarr", radarr_body(app, movieFile={"id": 31, "path": other, "sceneName": "Other.Name-GRP"}))
    assert job["path"] == app["film"] and job["release"] == "Film.A.1979.1080p.WEB-DL-GRP"
    (line,) = log_lines()
    assert line["source"] == "webhook" and line["result"] == "warning" and other in line["note"]


@pytest.mark.parametrize("body, api, code, why", [
    (dict(movieFile={"id": 99}), {}, 400, "has no file 99"),
    (dict(movie={"id": 8}), {}, 400, "belongs to movie 7, and the body names 8"),
    (dict(movie={"id": True}), {}, 400, "names no movie id and file id"),
    (dict(movie={"id": 0}), {}, 400, "names no movie id and file id"),
    (dict(movieFile={"id": "31"}), {}, 400, "names no movie id and file id"),
    (dict(movieFile=None), {}, 400, "names no movie id and file id"),
    (dict(downloadId="x" * 201), {}, 400, "downloadId"),
    (dict(downloadId=["a"]), {}, 400, "downloadId"),
    ({}, {"moviefile/31": {"id": 31, "movieId": 7, "path": "/data/../etc/passwd"}}, 400, "no plain absolute path"),
    (dict(deletedFiles=[{"path": "/etc/passwd"}]), {}, 400, "is not in the folder"),
    (dict(deletedFiles="/etc/passwd"), {}, 400, "no list of files"),
    (dict(deletedFiles=[{"path": "{movies}/../other/x.mkv"}]), {}, 400, "is not in the folder"),
    (dict(deletedFiles=[{"path": "{movies}/a|b.mkv"}]), {}, 400, "is not in the folder"),
    (dict(deletedFiles=[{"path": "{movies}/x\u0000.mkv"}]), {}, 400, "is not in the folder"),
    (dict(deletedFiles=[{"path": "{movies}/old.mkv", "recycleBinPath": "{movies}/../recycle/x\u001b.mkv"}]), {}, 400, "recycle bin"),
    ({}, {"moviefile/31": {"id": 31, "movieId": 7, "path": "/data/a\u0007b.mkv"}}, 400, "no plain absolute path"),
    (dict(deletedFiles=[{"path": "{movies} Extended/old.mkv"}]), {}, 400, "is not in the folder"),
    (dict(deletedFiles=[{"path": "{movies}/old.mkv", "recycleBinPath": "/etc/shadow"}]), {}, 400, "recycle bin"),
])
def test_a_body_the_api_does_not_back_is_refused(app, body, api, code, why):
    app["api"]["moviefile/99"] = http_error(404)
    app["api"].update(api)
    body = json.loads(json.dumps(body).replace("{movies}", app["movies"]))
    with pytest.raises(h.runner.Refused) as ex:
        arr_serve.download("radarr", radarr_body(app, **body))
    assert ex.value.code == code and why in str(ex.value)


def test_an_upgrade_maps_the_old_paths_and_the_bin_paths_of_the_app(app, monkeypatch, settings):
    """The app names its own paths in the body and in the API. The job holds the local ones, and the checks compare
    local with local."""
    settings(path_map=[("/app/movies", os.path.dirname(app["movies"])), ("/app/recycle", app["rbin"])])
    rel = os.path.relpath(app["film"], os.path.dirname(app["movies"]))
    app["api"].update({"moviefile/31": {"id": 31, "movieId": 7, "path": f"/app/movies/{rel}", "sceneName": "Film.A-GRP"},
                       "movie/7": {"id": 7, "path": "/app/movies/Film A (1979)"}, "config/mediamanagement": {"recycleBin": "/app/recycle"}})
    body = radarr_body(app, movieFile={"id": 31, "path": f"/app/movies/{rel}"},
                       deletedFiles=[{"path": "/app/movies/Film A (1979)/old.mkv", "recycleBinPath": "/app/recycle/Film A (1979)/old.mkv"}])
    job = arr_serve.download("radarr", body)
    assert job["path"] == app["film"] and not os.path.exists(h.CFG.log)   # the mapped body path is the API's: no warning
    assert (job["deleted"], job["recycled"]) == (os.path.join(app["movies"], "old.mkv"), os.path.join(app["rbin"], "Film A (1979)", "old.mkv"))


def test_an_upgrade_without_a_recycle_bin_keeps_empty_bin_paths(app):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}
    old = os.path.join(app["movies"], "old.mkv")
    job = arr_serve.download("radarr", radarr_body(app, deletedFiles=[{"path": old, "recycleBinPath": None}]))
    assert (job["deleted"], job["recycled"]) == (old, "")
    with pytest.raises(h.runner.Refused):   # with no bin, no recycle bin copy can pass
        arr_serve.download("radarr", radarr_body(app, deletedFiles=[{"path": old, "recycleBinPath": app["rbin"] + "/old.mkv"}]))


def test_an_import_complete_event_is_refused_with_what_to_change(app):
    with pytest.raises(h.runner.Refused) as ex:
        arr_serve.download("sonarr", sonarr_body(app, episodeFile=None, episodeFiles=[{"id": 41}]))
    assert ex.value.code == 400 and "On File Import" in str(ex.value)


def test_an_api_error_other_than_404_passes_up(app):
    app["api"]["moviefile/31"] = http_error(500)
    with pytest.raises(urllib.error.HTTPError):
        h.Event.from_webhook("radarr", radarr_body(app))


# --- PATH_MAP and the API key ---------------------------------------------------------------------------------------

def test_path_map_maps_both_ways_by_the_longest_whole_prefix(monkeypatch, settings):
    settings(path_map=[("/tv", "/media/tv"), ("/tv/kids", "/kids"), ("/", "/host")])
    assert h.mapped("/tv/Show/a.mkv", "sonarr") == "/media/tv/Show/a.mkv"
    assert h.mapped("/tv/kids/Show/a.mkv", "sonarr") == "/kids/Show/a.mkv"
    assert h.mapped("/tvshows/a.mkv", "sonarr") == "/host/tvshows/a.mkv"   # /tv matches whole folder names only
    assert h.mapped("/tv", "sonarr") == "/media/tv"
    assert h.mapped("/kids/Show/a.mkv", "sonarr", back=True) == "/tv/kids/Show/a.mkv"
    assert h.mapped("/media/tv/a.mkv", "sonarr", back=True) == "/tv/a.mkv"
    assert h.mapped({"a": ["/tv/x", 3, None, "Film /tv"], "b": {"c": "/tv/y"}}, "sonarr") == {"a": ["/media/tv/x", 3, None, "Film /tv"], "b": {"c": "/media/tv/y"}}
    settings(path_map=[])
    assert h.mapped("/tv/a.mkv", "sonarr") == "/tv/a.mkv"


def test_a_map_to_the_root_and_a_blank_query_value_keep_their_shape(monkeypatch, settings):
    settings(path_map=[("/tv", "/")])
    assert (h.mapped("/tv", "sonarr"), h.mapped("/tv/a/b.mkv", "sonarr"), h.mapped("/a/b.mkv", "sonarr", back=True)) == ("/", "/a/b.mkv", "/tv/a/b.mkv")
    settings(path_map=[("/app", "/local")])
    assert h.app_query("manualimport?q=&folder=%2Flocal%2Fa", "radarr") == "manualimport?q=&folder=%2Fapp%2Fa"


def test_arr_maps_the_answer_the_query_and_the_body(monkeypatch, settings, tmp_path):
    seen = []
    settings(path_map=[("/data", "/mnt/data")], radarr={"api_key": "k3y"})
    monkeypatch.setattr(h, "http", lambda url, method="GET", body=None, headers=None, timeout=15: seen.append((url, method, body, headers))
                        or {"path": "/data/movies/a.mkv", "folder": "/other/x"})
    assert h.arr("radarr", "parse?title=A&path=%2Fmnt%2Fdata%2Fmovies%2Fa.mkv") == {"path": "/mnt/data/movies/a.mkv", "folder": "/other/x"}
    assert seen[0][0].endswith("/api/v3/parse?title=A&path=%2Fdata%2Fmovies%2Fa.mkv") and seen[0][3] == {"X-Api-Key": "k3y"}
    h.arr_write("radarr", "command", "POST", {"name": "ManualImport", "files": [{"path": "/mnt/data/movies/a.mkv", "movieId": 7}]})
    assert seen[1][2] == {"name": "ManualImport", "files": [{"path": "/data/movies/a.mkv", "movieId": 7}]}


def test_the_api_key_comes_from_the_env_file_before_config_xml(monkeypatch, settings, tmp_path):
    conf = tmp_path / "config.xml"
    conf.write_text("<Config><ApiKey>fromxml</ApiKey></Config>")
    settings(sonarr={"dir": str(tmp_path), "api_key": ""})
    assert h.api_key("sonarr") == "fromxml"
    settings(sonarr={"api_key": "fromenv-0123456789"})
    assert h.api_key("sonarr") == "fromenv-0123456789"
    assert h.mask("key fromenv-0123456789") == "key <SONARR_API_KEY>"


def test_config_refuses_to_start_without_credentials_or_with_a_bad_setting(monkeypatch, settings):
    base = {"webhook_user": "guard", "webhook_password": "s3cret-pass", "path_map": [], "audit_time": "07:30"}
    settings(**base)
    assert arr_serve.listen_config() == (AUTH.encode(), "07:30")
    settings(audit_time="")
    assert arr_serve.listen_config()[1] == ""
    for k, v in (("webhook_password", ""), ("webhook_user", ""), ("webhook_user", "a:b"), ("webhook_password", "pässword"),
                 ("audit_time", "7.30")):
        settings(**{k: v})
        with pytest.raises(SystemExit):
            arr_serve.listen_config()
        settings(**{k: base[k]})
    settings(audit_time=h.settings("/nonexistent/arr-media-guard.env").audit_time)   # no AUDIT_TIME key
    assert arr_serve.listen_config()[1] == "07:30"   # the default, the time of the native timer
    settings(map_error="PATH_MAP takes pairs APP_PATH:LOCAL_PATH of absolute paths")
    with pytest.raises(SystemExit, match="PATH_MAP takes pairs"):   # a map that left a pair out would edit the wrong paths
        arr_serve.listen_config()


def webhook_env(tmp_path, monkeypatch, settings, text):
    """An env file that holds text, with mode 0640, as the listener's env file. CFG takes its Webhook pair."""
    env = tmp_path / "arr-media-guard.env"
    env.write_text(text)
    env.chmod(0o640)
    monkeypatch.setattr(arr_serve.config, "ENV_FILE", str(env))
    pair = h.env_file(str(env))
    settings(webhook_user=pair.get("WEBHOOK_USER", ""), webhook_password=pair.get("WEBHOOK_PASSWORD", ""), path_map=[], map_error=None)
    return env


def password_of(env):
    pair = h.env_file(str(env))
    return pair["WEBHOOK_USER"], pair["WEBHOOK_PASSWORD"]


@pytest.mark.parametrize("user_line, user", [("WEBHOOK_USER=''", "arr-admin"), ('WEBHOOK_USER="guard"', "guard")])
def test_an_empty_password_is_generated_into_the_env_file_in_place(tmp_path, monkeypatch, settings, capsys, user_line, user):
    """The listener writes a new password in place of the empty line, and the user arr-admin when that is empty too.
    A user already set keeps its line. The other lines and the mode stay. The log line never shows the password."""
    env = webhook_env(tmp_path, monkeypatch, settings, f"# the top\nLOG='/x.jsonl'\n{user_line}\nWEBHOOK_PASSWORD=''\n# the end\n")
    auth = arr_serve.listen_config()[0]
    got_user, pw = password_of(env)
    assert (got_user, len(pw)) == (user, 32) and re.fullmatch(r"[A-Za-z0-9_-]+", pw), pw   # token_urlsafe(24): no ':' and no quote
    assert auth == b"Basic " + base64.b64encode(f"{user}:{pw}".encode())
    lines = env.read_text().splitlines()
    assert lines == ["# the top", "LOG='/x.jsonl'", user_line if user == "guard" else "WEBHOOK_USER='arr-admin'", f"WEBHOOK_PASSWORD='{pw}'", "# the end"]
    assert oct(env.stat().st_mode & 0o7777) == oct(0o640) and [p.name for p in tmp_path.iterdir()] == [env.name]
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and pw not in out, out
    assert f"generated a Webhook password and wrote it to {env} as WEBHOOK_PASSWORD." in out, out
    assert "set Username to WEBHOOK_USER and Password to WEBHOOK_PASSWORD from that file." in out, out


def test_a_key_the_env_file_does_not_hold_is_added_and_each_start_gets_a_new_password(tmp_path, monkeypatch, settings):
    env = webhook_env(tmp_path, monkeypatch, settings, "LOG='/x.jsonl'")
    arr_serve.listen_config()
    user, first = password_of(env)
    assert env.read_text() == f"LOG='/x.jsonl'\nWEBHOOK_USER='arr-admin'\nWEBHOOK_PASSWORD='{first}'\n" and user == "arr-admin"
    seen = {first}
    for _ in range(3):
        env.write_text("WEBHOOK_PASSWORD=''\n")
        user, pw = arr_serve.new_password("guard")
        assert (user, env.read_text()) == ("guard", f"WEBHOOK_PASSWORD='{pw}'\n") and pw not in seen
        seen.add(pw)


@pytest.mark.parametrize("text", ["WEBHOOK_USER='guard'\nWEBHOOK_PASSWORD='s3cret-pass'\n", "WEBHOOK_USER=\"guard\"\nWEBHOOK_PASSWORD=s3cret-pass\n",
                                  "WEBHOOK_USER=''\nWEBHOOK_PASSWORD='s3cret-pass'\n", "WEBHOOK_PASSWORD='s3cret-pass'\n"])
def test_a_password_already_set_is_never_changed(tmp_path, monkeypatch, settings, capsys, text):
    """A pair already set starts the listener as it is. A user left empty with a password set keeps the refusal, and
    the file stays as it was."""
    env = webhook_env(tmp_path, monkeypatch, settings, text)
    before = env.stat()
    if "WEBHOOK_USER=''" in text or "WEBHOOK_USER" not in text:
        with pytest.raises(SystemExit, match="set WEBHOOK_USER and WEBHOOK_PASSWORD in "):
            arr_serve.listen_config()
    else:
        assert arr_serve.listen_config()[0] == AUTH.encode()
    assert env.read_text() == text and env.stat().st_mtime_ns == before.st_mtime_ns and "generated" not in capsys.readouterr().out


@pytest.mark.parametrize("step", ["fsync", "replace"])
def test_a_failed_write_leaves_the_env_file_whole_and_stops_with_why(tmp_path, monkeypatch, settings, capsys, step):
    """The new file goes to a temp file and a rename. A failed step leaves the old file and no temp file."""
    text = "LOG='/x.jsonl'\nWEBHOOK_USER='guard'\nWEBHOOK_PASSWORD=''\n"
    env = webhook_env(tmp_path, monkeypatch, settings, text)

    def fail(*a):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(arr_serve.os, step, fail)
    with pytest.raises(SystemExit, match=r"WEBHOOK_PASSWORD is empty, and the listener did not write a new one to .*No space left"):
        arr_serve.listen_config()
    assert env.read_text() == text and [p.name for p in tmp_path.iterdir()] == [env.name] and "generated" not in capsys.readouterr().out


def env_settings(tmp_path, monkeypatch, text, environ):
    """An env file that holds text, as the listener's env file, and CFG read from it under environ."""
    env = tmp_path / "arr-media-guard.env"
    env.write_text(text)
    monkeypatch.setattr(arr_serve.config, "ENV_FILE", str(env))
    monkeypatch.setattr(arr_serve.config, "CFG", h.settings(str(env), environ))
    return env


@pytest.mark.parametrize("environ", [{"WEBHOOK_PASSWORD": ""}, {"WEBHOOK_USER": "guard", "WEBHOOK_PASSWORD": "s3cret-pass"}])
def test_a_password_from_the_environment_is_never_generated_or_written(tmp_path, monkeypatch, capsys, environ):
    """The environment wins over the env file, so a password written there would never take effect. An empty one
    stops the listener with what to set."""
    text = "WEBHOOK_USER='arr-admin'\nWEBHOOK_PASSWORD=''\n"
    env = env_settings(tmp_path, monkeypatch, text, environ)
    if environ["WEBHOOK_PASSWORD"]:
        assert arr_serve.listen_config()[0] == AUTH.encode()
    else:
        with pytest.raises(SystemExit, match=f"set WEBHOOK_USER and WEBHOOK_PASSWORD in {re.escape(str(env))} or the environment"):
            arr_serve.listen_config()
    assert env.read_text() == text and [p.name for p in tmp_path.iterdir()] == [env.name] and "generated" not in capsys.readouterr().out


@pytest.mark.parametrize("user", ["", "guard"])
def test_a_user_from_the_environment_is_never_written(tmp_path, monkeypatch, capsys, user):
    """The listener still writes a new password into the env file, and says the user comes from the environment. An
    empty user from the environment stops it, and the file stays as it was."""
    env = env_settings(tmp_path, monkeypatch, "WEBHOOK_PASSWORD=''\n", {"WEBHOOK_USER": user})
    if not user:
        with pytest.raises(SystemExit, match="set WEBHOOK_USER and WEBHOOK_PASSWORD in "):
            arr_serve.listen_config()
        assert env.read_text() == "WEBHOOK_PASSWORD=''\n" and "generated" not in capsys.readouterr().out
        return
    auth = arr_serve.listen_config()[0]
    pw = h.env_file(str(env))["WEBHOOK_PASSWORD"]
    assert env.read_text() == f"WEBHOOK_PASSWORD='{pw}'\n" and len(pw) == 32
    assert auth == b"Basic " + base64.b64encode(f"guard:{pw}".encode())
    assert "set Username to WEBHOOK_USER from the environment and Password to WEBHOOK_PASSWORD from that file." in capsys.readouterr().out


def test_plex_takes_path_map_while_plex_path_map_is_unset(monkeypatch, settings):
    settings(path_map=[("/data", "/mnt/data")])
    item = {"ratingKey": "7101", "Guid": [{"id": "tmdb://1"}], "Media": [{"Part": [{"file": "/data/movies/A/a.mkv"}]}]}
    def fake_plex_get(path, **q):
        if path == "/library/sections":
            return {"Directory": [{"key": "12", "Location": [{"path": "/data/movies"}]}]}
        return {"Metadata": [item]}
    monkeypatch.setattr(h, "plex_get", fake_plex_get)
    assert h.plex_find("/mnt/data/movies/A/a.mkv", {"guids": ["tmdb://1"], "title": "A"}) == (["7101"], False, "12")
    p = {"path": "/mnt/data/movies/A"}
    assert h.plex_folder_scan(p) == (["folder"], False, "12")
    sent = []
    monkeypatch.setattr(h, "http", lambda url, *a, **k: sent.append(url))
    h.plex_folder_scan(dict(p, section="12"), send=True)
    assert "path=%2Fdata%2Fmovies%2FA&" in sent[0]


def test_each_program_maps_by_its_own_map_and_by_whole_folder_names(monkeypatch, settings):
    """"/mnt/TV" is a string prefix of "/mnt/TV Shows". A map takes whole folder names, and the longest prefix wins
    within each map, in both directions. A program without its own map takes PATH_MAP."""
    settings(path_map=[("/data", "/media")], sonarr={"path_map": [("/mnt/TV", "/media/TV")]},
             plex_path_map=[("/mnt/TV Shows", "/media/TV"), ("/mnt/TV", "/media/old")])
    assert h.mapped("/mnt/TV Shows/A/a.mkv", "sonarr") == "/mnt/TV Shows/A/a.mkv"   # no whole-name match
    assert h.mapped("/mnt/TV/A/a.mkv", "sonarr") == "/media/TV/A/a.mkv"
    assert h.mapped("/media/TV/A/a.mkv", "sonarr", back=True) == "/mnt/TV/A/a.mkv"
    assert h.mapped("/media/TV/A/a.mkv", "plex", back=True) == "/mnt/TV Shows/A/a.mkv"
    assert h.mapped("/mnt/TV Shows/A/a.mkv", "plex") == "/media/TV/A/a.mkv"
    assert h.mapped("/mnt/TV/A/a.mkv", "plex") == "/media/old/A/a.mkv"
    assert h.mapped("/media/TV Shows/a.mkv", "plex", back=True) == "/media/TV Shows/a.mkv"
    assert h.mapped("/data/a.mkv", "radarr") == "/media/a.mkv" and h.mapped("/data/a.mkv", "sonarr") == "/data/a.mkv"
    assert h.app_query("parse?path=%2Fmedia%2FTV%2Fa.mkv", "sonarr") == "parse?path=%2Fmnt%2FTV%2Fa.mkv"
    assert h.app_query("parse?path=%2Fmedia%2Fa.mkv", "radarr") == "parse?path=%2Fdata%2Fa.mkv"


TESTER = {"SONARR_PATH_MAP": "/mnt/TV:{m}/TV|/mnt/Anime:{m}/Anime", "RADARR_PATH_MAP": "/movies:{m}/Movies",
          "PLEX_PATH_MAP": "/mnt/TV Shows:{m}/TV|/mnt/Anime:{m}/Anime|/mnt/Movies:{m}/Movies"}


@pytest.fixture
def tester(tmp_path, monkeypatch, settings):
    """A setup with a path map per program: Sonarr sees TV at /mnt/TV, Radarr sees films at /movies, Plex sees them at
    /mnt/TV Shows and /mnt/Movies, and this container sees all three under its own media folder. Anime has one path in
    Sonarr and Plex. The real arr() runs against a fake HTTP of both apps and Plex. Returns the fake's state."""
    m = tmp_path / "media"
    ep, film = m / "TV" / "Show A" / "Season 1" / "Show A - S01E02.mkv", m / "Movies" / "Film A (1979)" / "Film A (1979).mkv"
    for f in (ep, film):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    (m / "Anime").mkdir()
    pairs = {k: h.path_map(k, v.format(m=m))[0] for k, v in TESTER.items()}
    settings(path_map=[], plex_path_map=pairs["PLEX_PATH_MAP"], plex_url="http://plex.invalid", plex_token="plex-t0ken-1234", log=str(tmp_path / "log.jsonl"),
             sonarr={"path_map": pairs["SONARR_PATH_MAP"], "url": "http://sonarr.invalid", "api_key": "ks"},
             radarr={"path_map": pairs["RADARR_PATH_MAP"], "url": "http://radarr.invalid", "api_key": "kr"})
    s = {"ep": str(ep), "film": str(film), "media": str(m), "calls": [], "api": {
        "sonarr/episodefile/41": {"id": 41, "seriesId": 5, "path": "/mnt/TV/Show A/Season 1/Show A - S01E02.mkv"},
        "sonarr/episode": [{"id": 901}],
        "sonarr/series/5": {"id": 5, "path": "/mnt/TV/Show A"},
        "sonarr/config/mediamanagement": {"recycleBin": ""},
        "sonarr/rootfolder": [{"path": "/mnt/TV"}, {"path": "/mnt/Anime"}],
        "radarr/moviefile/31": {"id": 31, "movieId": 7, "path": "/movies/Film A (1979)/Film A (1979).mkv"},
        "radarr/rootfolder": [{"path": "/movies"}],
        "sonarr/command": {"id": 1}, "radarr/parse": {},
        "plex/library/sections": {"MediaContainer": {"Directory": [
            {"key": "1", "Location": [{"path": "/mnt/TV Shows"}]}, {"key": "2", "Location": [{"path": "/mnt/Anime"}]},
            {"key": "3", "Location": [{"path": "/mnt/Movies"}]}, {"key": "4", "Location": [{"path": "/mnt/Music"}]}]}},
        "plex/library/sections/1/all": {"MediaContainer": {"Metadata": [{"ratingKey": "100", "Guid": [{"id": "tvdb://5"}]}]}},
        "plex/library/metadata/100/allLeaves": {"MediaContainer": {"Metadata": [
            {"ratingKey": "101", "Media": [{"Part": [{"file": "/mnt/TV Shows/Show A/Season 1/Show A - S01E02.mkv"}]}]}]}},
        "plex/library/sections/3/all": {"MediaContainer": {"Metadata": [
            {"ratingKey": "300", "Guid": [{"id": "tmdb://7"}], "Media": [{"Part": [{"file": "/mnt/Movies/Film A (1979)/Film A (1979).mkv"}]}]}]}},
    }}

    def fake_http(url, method="GET", body=None, headers=None, timeout=15):
        u = urllib.parse.urlsplit(url)
        who, path = u.netloc.split(".")[0], u.path.removeprefix("/api/v3")
        s["calls"].append((who, method, path, dict(urllib.parse.parse_qsl(u.query)), body))
        v = s["api"].get(f"{who}{path}")
        if isinstance(v, Exception) or v is None:
            raise v or http_error(404)
        return json.loads(json.dumps(v))
    monkeypatch.setattr(h, "http", fake_http)
    return s


def test_the_tester_setup_maps_each_import_by_its_app_and_finds_it_in_plex(tester):
    """A Sonarr import under /mnt/TV and a Radarr import under /movies reach the job as local paths. Plex lists each
    under its own path, and the lookup finds it there."""
    body = {"eventType": "Download", "series": {"id": 5}, "episodeFile": {"id": 41, "path": "/mnt/TV/Show A/Season 1/Show A - S01E02.mkv"},
            "deletedFiles": [{"path": "/mnt/TV/Show A/Season 1/old.mkv", "recycleBinPath": ""}]}
    job = arr_serve.download("sonarr", body)
    assert (job["path"], job["deleted"]) == (tester["ep"], os.path.join(os.path.dirname(tester["ep"]), "old.mkv"))
    assert not os.path.exists(h.CFG.log)   # the posted path maps to the API's: no warning
    film = arr_serve.download("radarr", {"eventType": "Download", "movie": {"id": 7}, "movieFile": {"id": 31, "path": "/movies/Film A (1979)/Film A (1979).mkv"}})
    assert film["path"] == tester["film"]
    rel = os.path.join(tester["media"], "TV", "downloads", "Show.A.S01E02.WEB-GRP")   # Sonarr sees it under /mnt/TV
    os.makedirs(rel)
    open(os.path.join(rel, "show.a.s01e02.web-grp.nfo"), "w").write("Title : Night Shift\n")
    sourced = arr_serve.download("sonarr", dict(body, episodeFile=dict(body["episodeFile"], sourcePath="/mnt/TV/downloads/Show.A.S01E02.WEB-GRP/show.a.s01e02.web-grp.mkv")))
    assert sourced["nfo_title"] == "Night Shift"   # the source path maps with Sonarr's map
    outside = os.path.join(os.path.dirname(tester["media"]), "outside", "Show.A.S01E02.WEB-GRP")   # under no folder of the map
    os.makedirs(outside)
    open(os.path.join(outside, "show.a.s01e02.web-grp.nfo"), "w").write("Title : Night Shift\n")
    posted = dict(body, episodeFile=dict(body["episodeFile"], sourcePath=os.path.join(outside, "show.a.s01e02.web-grp.mkv")))
    assert arr_serve.download("sonarr", posted)["nfo_title"] is None
    assert h.plex_find(job["path"], {"guids": ["tvdb://5"], "title": "Show A", "show": True}) == (["101"], False, "1")
    assert h.plex_find(film["path"], {"guids": ["tmdb://7"], "title": "Film A"}) == (["300"], False, "3")
    p = h.plex_folder_job("sonarr", "hook", "Show A", os.path.dirname(job["path"]), None)
    assert h.plex_folder_scan(p) == (["folder"], False, "1")
    h.plex_folder_scan(dict(p, section="1"), send=True)
    assert tester["calls"][-1][2:4] == ("/library/sections/1/refresh", {"path": "/mnt/TV Shows/Show A/Season 1", "X-Plex-Token": "plex-t0ken-1234"})
    h.arr_write("sonarr", "command", "POST", {"name": "RescanSeries", "path": os.path.dirname(job["path"])})
    h.arr("radarr", "parse?" + urllib.parse.urlencode({"path": film["path"]}))
    assert [c[3:] for c in tester["calls"][-2:]] == [({}, {"name": "RescanSeries", "path": "/mnt/TV/Show A/Season 1"}),
                                                    ({"path": "/movies/Film A (1979)/Film A (1979).mkv"}, None)]
    assert h.path_warnings() == []   # the Music library holds no root folder, so it never warns


def test_one_path_map_for_every_program_misses_plex_and_the_check_names_it(tester, monkeypatch, settings, capsys):
    """The tester's first setup: PATH_MAP alone. Plex lists TV under /mnt/TV Shows and films under /mnt/Movies, so the
    lookup finds neither, and the path check names each root folder and PLEX_PATH_MAP. --selftest and the listener
    start print the warnings and fail on none."""
    m = tester["media"]
    settings(path_map=[("/mnt/TV", f"{m}/TV"), ("/mnt/Anime", f"{m}/Anime"), ("/movies", f"{m}/Movies")], plex_path_map=[],
             sonarr={"path_map": []}, radarr={"path_map": []})
    assert h.plex_find(tester["ep"], {"guids": ["tvdb://5"], "title": "Show A", "show": True}) == ([], False, None)
    want = [f"no Plex library folder holds Sonarr's root folder {m}/TV, which PATH_MAP puts at /mnt/TV in Plex. "
            "Fix PATH_MAP, or set PLEX_PATH_MAP, so Plex finds the files arr-media-guard edits.",
            f"no Plex library folder holds Radarr's root folder {m}/Movies, which PATH_MAP puts at /movies in Plex. "
            "Fix PATH_MAP, or set PLEX_PATH_MAP, so Plex finds the files arr-media-guard edits."]
    assert sorted(h.path_warnings()) == sorted(want)
    arr_serve.path_check()
    assert sorted(capsys.readouterr().out.splitlines()) == sorted(f"arr-media-guard: warning: {w}" for w in want)
    h.main(["--selftest"])
    out = capsys.readouterr().out
    assert all(f"warning: {w}" in out for w in want) and out.rstrip().endswith("selftest ok")


def test_the_path_check_names_a_root_folder_this_script_does_not_see_and_its_setting(tester, monkeypatch, settings):
    tester["api"]["sonarr/rootfolder"].append({"path": "/mnt/TV/Kids"})   # mapped, and not there
    tester["api"]["radarr/rootfolder"].append({"path": "/films4k"})       # no pair maps it
    assert sorted(h.path_warnings()) == sorted([
        f"this script does not see {tester['media']}/TV/Kids, where SONARR_PATH_MAP puts Sonarr's root folder /mnt/TV/Kids. "
        "Mount the media there, or fix SONARR_PATH_MAP.",
        "this script does not see Radarr's root folder /films4k. Mount the media there, or fix RADARR_PATH_MAP.",
        "no Plex library folder holds Radarr's root folder /films4k. Fix PLEX_PATH_MAP, so Plex finds the files arr-media-guard edits."])
    assert h.path_warnings(per_app=False) == [   # the listener's start check names a root folder it does not see
        "no Plex library folder holds Radarr's root folder /films4k. Fix PLEX_PATH_MAP, so Plex finds the files arr-media-guard edits."]
    # A Plex library inside a root folder, or one that holds it, counts. Without PLEX_URL there is no Plex check.
    tester["api"]["plex/library/sections"]["MediaContainer"]["Directory"].append({"key": "5", "Location": [{"path": "/films4k/uhd"}]})
    assert all(w.startswith("this script does not see") for w in h.path_warnings())
    monkeypatch.setattr(h, "SERVE", True)   # docker exec --serve, or the listener
    assert all(w.startswith("this container does not see") for w in h.path_warnings())
    settings(plex_url="")
    tester["calls"].clear()
    assert len(h.path_warnings()) == 2 and not any(c[0] == "plex" for c in tester["calls"])


def test_with_no_map_set_the_path_check_says_to_set_one(tester, settings):
    """No map moves the root folders, so the warnings name no map that puts them anywhere, and they say to set one."""
    settings(plex_path_map=[], sonarr={"path_map": []}, radarr={"path_map": [], "api_key": "", "dir": "/nonexistent"})   # Sonarr alone
    tester["api"]["sonarr/rootfolder"] = [{"path": "/mnt/TV"}]
    assert h.path_warnings() == [
        "this script does not see Sonarr's root folder /mnt/TV. Mount the media there, or set SONARR_PATH_MAP or PATH_MAP.",
        "no Plex library folder holds Sonarr's root folder /mnt/TV. Set PLEX_PATH_MAP or PATH_MAP, so Plex finds the files arr-media-guard edits."]


def test_a_pair_with_trailing_slashes_maps_the_root_folder_itself(tester, settings):
    """SONARR_PATH_MAP='/mnt/TV/:/media/TV/' maps /mnt/TV, so the Test event finds each root folder and passes."""
    settings(sonarr={"path_map": h.path_map("SONARR_PATH_MAP", "/mnt/TV/:{m}/TV/|/mnt/Anime//:{m}/Anime/".format(m=tester["media"]))[0]})
    assert h.mapped("/mnt/TV", "sonarr") == f"{tester['media']}/TV" and h.mapped(f"{tester['media']}/Anime", "sonarr", True) == "/mnt/Anime"
    assert h.app_check("sonarr")[0] is None and h.path_warnings() == []


def test_the_path_check_says_when_an_app_or_plex_does_not_answer(tester, monkeypatch, settings, capsys):
    tester["api"]["sonarr/rootfolder"] = urllib.error.URLError("refused")
    tester["api"]["plex/library/sections"] = urllib.error.URLError("http://plex.invalid/library/sections?X-Plex-Token=plex-t0ken-1234 refused")
    assert h.path_warnings() == ["Sonarr did not answer, so its root folders are not checked: URLError: <urlopen error refused>",
                                 "Plex did not answer, so its library folders are not checked: URLError: "
                                 "<urlopen error http://plex.invalid/library/sections?X-Plex-Token=<PLEX_TOKEN> refused>"]
    arr_serve.path_check()
    assert "Sonarr did not answer" in capsys.readouterr().out
    arr_serve.path_check(per_app=False)   # the listener's start check reports an app that does not answer
    assert "Sonarr did not answer" not in capsys.readouterr().out
    settings(radarr={"api_key": "", "dir": "/nonexistent"})   # an app this host does not run is never asked
    tester["calls"].clear()
    h.path_warnings()
    assert not any(c[0] == "radarr" for c in tester["calls"])


# --- the HTTP handler --------------------------------------------------------------------------------------------------

def settled(srv, slots):
    """Wait until every request of srv ended, its finish() included: take every slot, then give them all back."""
    got = [srv.slots.acquire(timeout=10) for _ in range(slots)]
    for ok in got:
        if ok:
            srv.slots.release()
    assert all(got), "a request did not end"


@pytest.fixture
def server(app, monkeypatch):
    """The real handler on a free local port. send() makes one request and serves it."""
    monkeypatch.setattr(arr_serve.Handler, "auth", AUTH.encode())
    srv = arr_serve.Server(("127.0.0.1", 0), arr_serve.Handler)
    srv.queued = False
    slots = arr_serve.THREADS + arr_serve.BACKLOG

    def send(method, path, body=None, headers=None, raw=None):
        t = threading.Thread(target=srv.handle_request)
        t.start()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        data = raw if raw is not None else None if body is None else json.dumps(body).encode()
        c.request(method, path, body=data, headers={"Authorization": AUTH, **(headers or {})})
        r = c.getresponse()
        out = (r.status, r.read().decode(), dict(r.getheaders()))
        c.close()
        t.join(20)
        settled(srv, slots)   # the client has its answer before the pool thread counts a refusal
        assert srv.open == set()   # a connection that closed leaves the open set
        return out
    yield send
    srv.server_close()


def test_a_download_post_queues_the_job_and_asks_for_a_worker(server, app, capsys):
    code, text, _ = server("POST", "/radarr", radarr_body(app))
    assert code == 200 and text.strip() == f"arr-media-guard: queued {app['film']}"
    assert arr_serve.REFUSALS.n == 0 and '"POST /radarr HTTP/1.1" 200' in capsys.readouterr().out   # an app's post gets its line
    (name,) = h.queued()
    assert h.job_of(name)["file_id"] == "31"
    assert not os.path.exists(h.CFG.log)   # a queued job leaves no decision line of its own


STORE_WORKER = """
import contextlib, json, sqlite3, sys
with contextlib.closing(sqlite3.connect(sys.argv[1], isolation_level=None)) as db:
    rows = db.execute("SELECT job FROM jobs").fetchall()
    db.execute("DELETE FROM jobs")
print(json.dumps([json.loads(j)["path"] for j, in rows]))
"""


def test_a_worker_gets_each_of_two_downloads_in_a_row(server, app):
    """The listener's loop and its request threads each hold a connection to the store. A worker in another process
    takes each job and closes its connection. The second job must reach the next worker too, so no request thread
    may drop the store locks of the listener, see store.db()."""
    h.store.db()   # the connection of the listener's loop, see runner.ensure_worker()
    take = lambda: json.loads(subprocess.run([sys.executable, "-c", STORE_WORKER, h.store.path()], check=True, capture_output=True,
                                             text=True).stdout)
    assert server("POST", "/radarr", radarr_body(app))[0] == 200
    assert take() == [app["film"]]
    assert server("POST", "/sonarr", sonarr_body(app))[0] == 200
    assert take() == [app["ep"]]


@pytest.mark.parametrize("auth", [None, "Basic " + base64.b64encode(b"guard:wrong").decode(), "Bearer x", AUTH + "x"])
def test_a_post_without_the_right_credentials_is_refused_before_the_body(server, app, auth):
    headers = {"Authorization": auth} if auth else {"Authorization": ""}
    code, text, hdrs = server("POST", "/radarr", radarr_body(app), headers=headers)
    assert code == 401 and hdrs.get("WWW-Authenticate") == 'Basic realm="arr-media-guard"'
    assert h.queued() == [] and app["calls"] == [] and not os.path.exists(h.CFG.log)   # counted, never logged
    assert (arr_serve.REFUSALS.n, arr_serve.REFUSALS.last) == (1, "127.0.0.1")


def test_an_oversized_body_is_refused_unread(server, app):
    started = time.monotonic()
    code, text, _ = server("POST", "/radarr", headers={"Content-Length": str(arr_serve.MAX_BODY + 1)}, raw=b"")
    assert code == 413 and time.monotonic() - started < arr_serve.READ_TIMEOUT   # it never waited for the body
    assert h.queued() == [] and app["calls"] == []


def test_a_body_at_the_limit_is_read(server, app, monkeypatch):
    raw = json.dumps(radarr_body(app)).encode()
    monkeypatch.setattr(arr_serve, "MAX_BODY", len(raw))
    assert server("POST", "/radarr", raw=raw)[0] == 200


@pytest.mark.parametrize("path, headers, raw, code", [
    ("/lidarr", {}, b"{}", 404),
    ("/radarr/x", {}, b"{}", 404),
    ("/radarr", {}, b"not json", 400),
    ("/radarr", {"Content-Length": "-1"}, b"", 400),
    ("/radarr", {}, b"[1, 2]", 400),
    ("/radarr", {}, b"{}", 200),   # no event type: ignored, as hook() ignores other events
])
def test_a_malformed_request_is_refused(server, app, path, headers, raw, code):
    assert server("POST", path, headers=headers, raw=raw)[0] == code
    assert h.queued() == []


def test_a_post_without_a_length_is_refused(app, server, monkeypatch):
    srv = arr_serve.http.server.HTTPServer(("127.0.0.1", 0), arr_serve.Handler)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    with socket.create_connection(srv.server_address, timeout=20) as c:
        c.sendall(f"POST /radarr HTTP/1.1\r\nHost: x\r\nAuthorization: {AUTH}\r\n\r\n".encode())
        assert c.recv(100).startswith(b"HTTP/1.0 411")
    t.join(20)
    srv.server_close()


def test_other_events_are_answered_and_ignored(server, app):
    code, text, _ = server("POST", "/sonarr", {"eventType": "Grab", "series": {"id": 5}})
    assert code == 200 and "Grab ignored" in text and h.queued() == [] and app["calls"] == []


def test_the_test_event_checks_the_policy_the_api_and_the_root_folders(server, app, monkeypatch):
    code, text, _ = server("POST", "/sonarr", {"eventType": "Test", "series": {"id": 1, "path": "C:\\testpath"}})
    assert code == 200 and "Test ok" in text and h.queued() == []
    app["api"]["rootfolder"].append({"path": "/nonexistent/anime"})
    code, text, _ = server("POST", "/sonarr", {"eventType": "Test"})
    assert code == 500 and "/nonexistent/anime" in text and "Mount the media at the app's paths, or set SONARR_PATH_MAP or PATH_MAP" in text
    roots, app["api"]["rootfolder"] = app["api"]["rootfolder"][:2], urllib.error.URLError("refused")
    code, text, _ = server("POST", "/sonarr", {"eventType": "Test"})
    assert code == 500 and "API did not answer" in text
    app["api"]["rootfolder"] = roots
    monkeypatch.setattr(h.arr_decide, "POLICY", None)
    code, text, _ = server("POST", "/sonarr", {"eventType": "Test"})
    assert code == 500 and "no policy loaded" in text


def test_a_refused_download_answers_why_and_logs_it(server, app):
    code, text, _ = server("POST", "/radarr", radarr_body(app, movie={"id": 8}))
    assert code == 400 and "belongs to movie 7" in text and h.queued() == []
    (line,) = log_lines()
    assert line["source"] == "webhook" and line["app"] == "radarr" and line["result"] == "refused" and "movie 7" in line["note"]


@pytest.mark.parametrize("fail", ["moviefile/31", "movie/7"])
def test_an_import_the_api_does_not_answer_is_queued_and_the_worker_asks_again(server, app, monkeypatch, settings, fail):
    """The job holds the ids and the body, and one line says why. The worker runs the checks and lookups of the
    listener, so it finds the file and the old files of the upgrade, and the body's checks still hold."""
    settings(keep_replaced=True)
    old = os.path.join(app["movies"], "Film A (1979) HDTV-720p.mkv")
    body = radarr_body(app, isUpgrade=True, deletedFiles=[{"id": 30, "path": old, "recycleBinPath": None}])
    answers, app["api"][fail] = app["api"][fail], urllib.error.URLError("refused")
    code, text, _ = server("POST", "/radarr", body)
    assert (code, text) == (200, "arr-media-guard: queued Radarr file 31\n")
    (line,) = log_lines()
    assert (line["source"], line["result"]) == ("webhook", "warning") and line["note"].startswith("the Radarr API did not answer: URLError")
    (name,) = h.queued()
    job = h.job_of(name)
    assert (job["path"], job["owner"], job["file_id"], job["download_id"], job["deleted"], job["webhook"]) == (None, "7", "31", "SABnzbd_nzo_abc123", None, body)
    for bad in (dict(body, downloadId=["a"]), dict(body, deletedFiles="/etc/passwd"), dict(body, movie={"id": True})):
        assert server("POST", "/radarr", bad)[0] == 400   # the body checks hold while the API fails
    assert h.queued() == [name]
    claims = []
    monkeypatch.setattr(h, "kept_replaced", lambda path, down=None, app=None: claims.append((path, down)))
    monkeypatch.setattr(h, "process", lambda ctx: pytest.fail("the API still fails"))
    h.run_job(name, [])   # the API still fails: the job goes back to the queue for a try a minute later
    ((later, due),) = h.store.read("SELECT name, due FROM jobs")
    assert 59 * 10**9 < due - time.time_ns() <= 60 * 10**9 and h.queued() == [] and not h.waiting() and claims == []
    assert log_lines()[-1]["note"].endswith("The job goes back to the queue, and the next try runs in 60 seconds")
    h.queue_job({"app": "radarr", "event": "Download", "time": time.time(), "path": "/nonexistent/x.mkv", "file_id": "1"})
    (behind,) = h.queued()   # a job queued after it runs first
    h.drop_job(behind)
    monkeypatch.setattr(h, "load_plex", lambda: [])
    h.worker(h.try_lock("worker.lock"))   # nothing is due, so the worker ends at once
    assert h.store.read("SELECT name FROM jobs") == [(later,)]
    real = time.time_ns
    monkeypatch.setattr(h.time, "time_ns", lambda: real() + 61 * 10**9)
    app["api"][fail] = answers
    seen = []
    monkeypatch.setattr(h, "process", lambda ctx: seen.append((ctx.path, ctx.job["time"])) or {"app": ctx.app, "path": ctx.path, "result": "no change"})
    monkeypatch.setattr(h.Radarr, "item", lambda self, owner, fid: ("Film A (1979)", "English", 120, {"guids": []}, False, {}))
    assert h.queued() == [later]
    h.run_job(later, [])
    assert seen == [(app["film"], job["time"])] and claims == [(old, "SABnzbd_nzo_abc123")]   # the job keeps its age
    assert log_lines()[-1]["path"] == app["film"] and log_lines()[-1]["result"] == "no change"


def test_a_webhook_job_whose_api_never_answers_is_dropped_once_at_the_age_limit(app, monkeypatch):
    """Each try waits twice as long as the one before, up to an hour. A job older than a day gets one error line."""
    clock = [time.time()]
    monkeypatch.setattr(h.time, "time", lambda: clock[0])
    monkeypatch.setattr(h.time, "time_ns", lambda: int(clock[0] * 10**9))
    monkeypatch.setattr(h, "process", lambda *a, **k: pytest.fail("the API never answered"))
    app["api"]["moviefile/31"] = urllib.error.URLError("refused")
    h.queue_job(arr_serve.download("radarr", radarr_body(app)))
    waits = []
    for _ in range(40):   # a day takes about 30 tries
        if not h.store.read("SELECT due FROM jobs"):
            break
        ((due,),) = h.store.read("SELECT due FROM jobs")
        clock[0] = max(clock[0], due / 10**9)   # the time of the next try
        (name,) = h.queued()
        h.run_job(name, [])
        waits.append(log_lines()[-1].get("note", "").rpartition("runs in ")[2])
    assert waits[:8] == [f"{w} seconds" for w in (60, 120, 240, 480, 960, 1920, 3600, 3600)] and waits[-1] == "" and len(waits) < 40
    (dropped,) = [r for r in log_lines() if r.get("outcome")]
    assert dropped["result"] == ("error: the Radarr API did not answer for a day, so the import was never checked. The last try: URLError: "
                                 "<urlopen error refused>") and dropped["job"] == name


@pytest.mark.parametrize("api_down", [False, True])
def test_an_import_this_container_does_not_see_waits_for_its_file_for_a_day(app, monkeypatch, api_down):
    """A missing mount or a slow NFS cache hides the file at the event, or when the API answers again. The job waits for
    the file as for the API, and it runs once the file shows. A file that never shows gives one error line at
    JOB_MAX_AGE. A file the app no longer has drops the job at once."""
    clock = [time.time()]
    monkeypatch.setattr(h.time, "time", lambda: clock[0])
    monkeypatch.setattr(h.time, "time_ns", lambda: int(clock[0] * 10**9))
    seen = []
    monkeypatch.setattr(h, "process", lambda ctx: seen.append(ctx.path) or {"app": ctx.app, "path": ctx.path, "result": "no change"})
    monkeypatch.setattr(h.Radarr, "item", lambda self, owner, fid: ("Film A (1979)", "English", 120, {"guids": []}, False, {}))
    os.rename(app["film"], app["film"] + ".hidden")
    answers = app["api"]["moviefile/31"]
    if api_down:
        app["api"]["moviefile/31"] = urllib.error.URLError("refused")
    job = arr_serve.download("radarr", radarr_body(app))
    assert job.get("unseen") is (None if api_down else True)
    app["api"]["moviefile/31"] = answers

    def look():   # one try of the worker at the time it is due
        ((due,),) = h.store.read("SELECT due FROM jobs")
        clock[0] = max(clock[0], due / 10**9)
        (name,) = h.queued()
        h.run_job(name, [])

    h.queue_job(job)
    look()
    assert h.job_of(h.store.read("SELECT name FROM jobs")[0][0])["unseen"] is True and seen == []
    assert log_lines()[-1]["note"] == (f"Radarr lists file 31 at {app['film']}, and this container does not see it there. The job goes back "
                                       "to the queue, and the next try runs in 60 seconds")
    look()
    assert log_lines()[-1]["note"].endswith("the next try runs in 120 seconds") and seen == []
    os.rename(app["film"] + ".hidden", app["film"])   # the mount shows the file
    look()
    assert seen == [app["film"]] and h.store.read("SELECT name FROM jobs") == []
    os.rename(app["film"], app["film"] + ".hidden")
    job = arr_serve.download("radarr", radarr_body(app))
    h.queue_job(job)
    clock[0] = job["time"] + h.JOB_MAX_AGE + 1
    look()
    assert log_lines()[-1]["result"] == (f"error: Radarr lists file 31 at {app['film']}, and this container does not see it there. A day "
                                         "passed, so the import was never checked") and h.store.read("SELECT name FROM jobs") == []
    app["api"]["moviefile/31"] = http_error(404)
    h.queue_job(dict(job, time=clock[0]))
    look()
    assert log_lines()[-1]["outcome"] == "file_gone" and h.store.read("SELECT name FROM jobs") == [] and seen == [app["film"]]


def test_a_stop_in_a_webhook_job_leaves_one_job_and_claims_the_kept_copies_at_most_once(app, monkeypatch, settings):
    """A stop at the change that gives the job its next try and its later time, then a stop after the job drops the body
    and before the claim. Each leaves one row of the job, and the next run neither runs a second copy nor claims twice."""
    settings(keep_replaced=True)
    old = os.path.join(app["movies"], "Film A (1979) HDTV-720p.mkv")
    answers, app["api"]["moviefile/31"] = app["api"]["moviefile/31"], urllib.error.URLError("refused")
    h.queue_job(arr_serve.download("radarr", radarr_body(app, isUpgrade=True, deletedFiles=[{"id": 30, "path": old, "recycleBinPath": None}])))
    rows = lambda: [(n, json.loads(j)) for n, j in h.store.read("SELECT name, job FROM jobs")]
    claims, seen, real_write, real_claim = [], [], h.store.write, h.claim_kept
    monkeypatch.setattr(h, "kept_replaced", lambda path, down=None, app=None: claims.append((path, down)))
    monkeypatch.setattr(h.store, "write", lambda sql, *a: (_ for _ in ()).throw(SystemExit(143)) if sql.startswith("UPDATE jobs SET name")
                        else real_write(sql, *a))
    (name,) = h.queued()
    with pytest.raises(SystemExit):
        h.run_job(name, [])
    ((n, job),) = rows()
    assert n == name and "tries" not in job and "webhook" in job   # the change did not happen, and the job is whole
    monkeypatch.setattr(h.store, "write", real_write)
    h.run_job(name, [])   # the next run asks again, and the API still fails
    ((later, job),) = rows()
    assert later != name and job["tries"] == 1 and h.queued() == []
    app["api"]["moviefile/31"] = answers
    real_ns = time.time_ns
    monkeypatch.setattr(h.time, "time_ns", lambda: real_ns() + 200 * 10**9)
    (name,) = h.queued()
    monkeypatch.setattr(h, "claim_kept", lambda job: (_ for _ in ()).throw(SystemExit(143)))
    with pytest.raises(SystemExit):
        h.run_job(name, [])
    ((n, job),) = rows()
    assert n == name and "webhook" not in job and job["path"] == app["film"]
    monkeypatch.setattr(h, "claim_kept", real_claim)
    monkeypatch.setattr(h, "process", lambda ctx: seen.append(ctx.path) or {"app": ctx.app, "path": ctx.path, "result": "no change"})
    monkeypatch.setattr(h.Radarr, "item", lambda self, owner, fid: ("Film A (1979)", "English", 120, {"guids": []}, False, {}))
    h.run_job(name, [])
    assert seen == [app["film"]] and claims == [] and rows() == []


def test_a_webhook_job_whose_item_is_gone_is_dropped_at_once(server, app, monkeypatch):
    """A 404 for the item means the app deleted it. The listener refuses the post, and a queued job ends with one line."""
    app["api"]["movie/7"] = http_error(404)
    body = radarr_body(app, isUpgrade=True, deletedFiles=[{"id": 30, "path": os.path.join(app["movies"], "old.mkv"), "recycleBinPath": None}])
    assert server("POST", "/radarr", body)[:2] == (400, "arr-media-guard: Radarr has no movie 7\n") and h.queued() == []
    h.queue_job(dict(h.Event.from_webhook("radarr", body, ask=False).job(), webhook=body))   # queued while the API failed
    (name,) = h.queued()
    h.run_job(name, [])
    assert h.store.read("SELECT name FROM jobs") == [] and log_lines()[-1]["result"] == "error: Refused: Radarr has no movie 7"


def test_an_import_this_container_does_not_see_is_queued_with_one_line(server, app):
    """The worker looks for the file by its id, see moved(), so a slow mount never loses the import."""
    app["api"]["moviefile/31"] = dict(app["api"]["moviefile/31"], path="/nonexistent/Film A.mkv")
    code, text, _ = server("POST", "/radarr", radarr_body(app, movieFile={"id": 31, "path": "/nonexistent/Film A.mkv"}))
    assert (code, text) == (200, "arr-media-guard: queued /nonexistent/Film A.mkv\n") and len(h.queued()) == 1
    (line,) = log_lines()
    assert line["result"] == "warning" and line["note"].startswith("Radarr lists file 31 at /nonexistent/Film A.mkv, and this container does not see it")


def test_a_put_queues_like_a_post(server, app):
    assert server("PUT", "/radarr", radarr_body(app))[0] == 200 and len(h.queued()) == 1


def test_a_length_of_other_digits_is_refused(server, app):
    code, text, _ = server("POST", "/radarr", headers={"Content-Length": "\u00b2"}, raw=b"")
    assert code == 400 and "no number" in text and log_lines()[0]["result"] == "refused"


def test_a_slow_body_is_refused_after_the_read_timeout(app, server, monkeypatch):
    assert arr_serve.Handler.timeout == arr_serve.READ_TIMEOUT   # every read of a client waits at most this long
    monkeypatch.setattr(arr_serve.Handler, "timeout", 0.5)
    srv = arr_serve.Server(("127.0.0.1", 0), arr_serve.Handler)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    started = time.monotonic()
    with socket.create_connection(srv.server_address, timeout=20) as c:
        c.sendall(f"POST /radarr HTTP/1.1\r\nHost: x\r\nAuthorization: {AUTH}\r\nContent-Length: 100\r\n\r\n{{}}".encode())
        assert c.recv(100).startswith(b"HTTP/1.0 408") and time.monotonic() - started < 5
    t.join(20)
    srv.server_close()
    (line,) = log_lines()
    assert line["result"] == "refused" and line["code"] == 408


def test_a_body_short_by_one_byte_is_refused(app, monkeypatch):
    monkeypatch.setattr(arr_serve.Handler, "auth", AUTH.encode())
    srv = arr_serve.Server(("127.0.0.1", 0), arr_serve.Handler)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    raw = b'{"eventType": "Test"}'   # whole JSON, so only the length check refuses it
    with socket.create_connection(srv.server_address, timeout=20) as c:
        c.sendall(f"POST /radarr HTTP/1.1\r\nHost: x\r\nAuthorization: {AUTH}\r\nContent-Length: {len(raw) + 1}\r\n\r\n".encode() + raw)
        c.shutdown(socket.SHUT_WR)
        assert c.recv(100).startswith(b"HTTP/1.0 408")
    t.join(20)
    srv.server_close()


def test_an_api_key_never_reaches_the_answer_or_the_log(server, app, monkeypatch, settings, capsys):
    settings(radarr={"api_key": "s3cretkey1234"})
    app["api"]["moviefile/31"] = urllib.error.URLError("refused, key s3cretkey1234")
    code, text, _ = server("POST", "/radarr", radarr_body(app))   # queued, with a warning line
    assert code == 200 and "s3cretkey1234" not in text
    monkeypatch.setattr(h, "queue_job", lambda job: (_ for _ in ()).throw(OSError("no space, key s3cretkey1234")))
    code, text, _ = server("POST", "/radarr", radarr_body(app))
    assert code == 502 and "<RADARR_API_KEY>" in text and "s3cretkey1234" not in text
    app["api"]["moviefile/31"] = h.runner.Refused(400, "the key s3cretkey1234 was named")
    server("POST", "/radarr", radarr_body(app))
    lines, out = open(h.CFG.log).read(), capsys.readouterr().out
    assert "s3cretkey1234" not in lines + out and lines.count("<RADARR_API_KEY>") == 4 and out.count("<RADARR_API_KEY>") == 4


def test_a_log_that_does_not_take_the_line_still_answers(server, app, monkeypatch, capsys):
    monkeypatch.setattr(h, "log", lambda rec: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert server("POST", "/radarr", radarr_body(app, movie={"id": 8}))[0] == 400
    assert "the decision log did not take the line: [Errno 28] No space left on device" in capsys.readouterr().out


def test_the_healthcheck_needs_no_credentials(server):
    assert server("GET", "/health", headers={"Authorization": ""})[:2] == (200, "ok\n")
    assert server("GET", "/radarr")[0] == 404


def test_a_healthcheck_that_passes_writes_no_access_line(server, app, capsys):
    """Docker and the host ask /health at each interval. The owner saw these lines fill the container log of 2.1.0:
    arr-media-guard: 127.0.0.1 "GET /health HTTP/1.1" 200 -
    A healthcheck that fails keeps its line, and so does every other request."""
    capsys.readouterr()
    assert server("GET", "/health")[:2] == (200, "ok\n")
    assert capsys.readouterr().out == ""
    server("POST", "/radarr", {"eventType": "Test"})
    assert '"POST /radarr HTTP/1.1" 200' in capsys.readouterr().out
    hd = arr_serve.Handler.__new__(arr_serve.Handler)   # a /health answer the listener never gives today
    hd.command, hd.path, hd.requestline, hd.quiet, hd.client_address = "GET", "/health", "GET /health HTTP/1.1", False, ("127.0.0.1", 1)
    hd.log_request(503)
    assert capsys.readouterr().out == 'arr-media-guard: 127.0.0.1 "GET /health HTTP/1.1" 503 -\n'


# --- the worker and the daily jobs --------------------------------------------------------------------------------------

def test_the_listener_starts_a_worker_only_when_work_waits_and_no_worker_runs(app, monkeypatch):
    started = []
    spawn = lambda what: started.append((what, h.try_lock("worker.lock") is not None)) or 4242
    monkeypatch.setattr(h.os, "fork", lambda: pytest.fail("the listener never forks"))
    assert h.ensure_worker(spawn) is None and started == []   # no work
    h.store.write("INSERT INTO jobs (name, at, job) VALUES ('1-1.json', 0, '{}')")
    held = h.try_lock("worker.lock")
    assert h.ensure_worker(spawn) is None and started == []   # a worker runs
    held.close()
    assert h.ensure_worker(spawn) == 4242 and started == [("worker", True)]   # the new worker takes the lock itself


def test_the_start_check_asks_an_app_again_then_warns_and_the_listener_goes_on(app, monkeypatch, settings, capsys):
    """An app that starts beside the listener is asked again for START_WAIT seconds. The check prints each result and
    never stops the listener. An app with no API key is not checked."""
    settings(radarr={"api_key": "radarr-key-1"}, sonarr={"api_key": "", "dir": "/nonexistent"})
    monkeypatch.setattr(arr_serve, "START_WAIT", 0.5)
    monkeypatch.setattr(h, "ASK_AGAIN", 0.05)
    real, roots = h.arr, app["api"]["rootfolder"]
    def arr(a, p):   # the app answers at the fourth ask: the path check asks once, then the start check
        if p == "rootfolder" and len([c for c in app["calls"] if c[1] == p]) < 3:
            app["calls"].append((a, p))
            raise urllib.error.URLError("refused")
        return real(a, p)
    monkeypatch.setattr(h, "arr", arr)
    arr_serve.start_check()
    out = capsys.readouterr().out.splitlines()
    assert out == ["arr-media-guard: radarr start check: ok"]   # the path check leaves an app that does not answer to it
    assert [c for c in app["calls"] if c[1] == "rootfolder"] == [("radarr", "rootfolder")] * 5   # 3 refused, then the check and the bin warnings
    app["api"]["rootfolder"], app["calls"][:] = urllib.error.URLError("refused"), []
    started = time.monotonic()
    arr_serve.start_check()   # it returns, so the listener goes on
    assert 0.5 <= time.monotonic() - started < 3
    assert capsys.readouterr().out.splitlines()[-1] == ("arr-media-guard: radarr warning: the start check failed: the Radarr API did not answer: "
                                                        "URLError: <urlopen error refused>")
    assert len([c for c in app["calls"] if c[1] == "rootfolder"]) > 3 and not os.path.exists(h.CFG.log)
    app["api"]["rootfolder"] = roots + [{"path": "/nonexistent/anime"}]
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}
    arr_serve.start_check()
    out = capsys.readouterr().out
    assert "arr-media-guard: radarr warning: the start check failed: this script does not see the root folders /nonexistent/anime" in out
    assert "does not see Radarr's root folder" not in out   # one line, from the start check
    app["api"]["rootfolder"] = roots
    settings(radarr={"api_key": "radarr-key-1"}, sonarr={"api_key": "sonarr-key-1"})
    monkeypatch.setattr(h.arr_decide, "POLICY", None)
    arr_serve.start_check()   # the policy fails once, and each app is still checked
    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if "no policy loaded" in line] == [f"arr-media-guard: warning: the start check failed: {h.policy_help()}"]
    assert "arr-media-guard: radarr start check: ok" in out and "arr-media-guard: sonarr start check: ok" in out


def test_the_start_check_names_an_app_with_its_own_url_and_no_api_key(app, settings, capsys):
    """A container with SONARR_URL and no config.xml mount needs SONARR_API_KEY, and the start check says so. An app
    with the shipped URL is one the user did not set up, and it stays quiet."""
    settings(radarr={"api_key": "", "dir": "/nonexistent"}, sonarr={"api_key": "", "dir": "/nonexistent", "url": "http://sonarr.lan:8989"})
    arr_serve.start_check()
    assert capsys.readouterr().out.splitlines() == [
        "arr-media-guard: sonarr warning: SONARR_URL is set, but the Sonarr API key does not read: [Errno 2] No such file or directory: "
        "'/nonexistent/config.xml'. Set SONARR_API_KEY."] and app["calls"] == []


def test_the_test_checks_wait_10_seconds_for_each_api_call(tester, monkeypatch):
    """A hung app fails the Test of the hook and of the listener in seconds. Every other call keeps its 60 seconds."""
    seen, fake = [], h.http
    def http(url, method="GET", body=None, headers=None, timeout=15):
        seen.append((url.split("/api/v3/")[1], timeout))
        return fake(url, method, body, headers, timeout)
    monkeypatch.setattr(h, "http", http)
    assert h.app_check("sonarr")[0] is None
    h.arr("sonarr", "series/5")
    assert seen == [("rootfolder", 10), ("config/mediamanagement", 10), ("rootfolder", 10), ("series/5", 60)]


def test_the_banner_is_the_approved_text_and_never_prints_for_the_worker_or_the_daily_jobs(app, monkeypatch, capsys):
    """The owner approved the banner byte for byte. The listener prints it, see
    test_the_listener_prints_each_map_and_checks_the_paths_at_its_start."""
    assert hashlib.sha256(arr_serve.BANNER.encode()).hexdigest() == "5957819f8847192c271f6877c1286770d63271f63c575fe70939810af8c92111"
    monkeypatch.setattr(h, "SERVE", False)
    monkeypatch.setattr(h, "worker", lambda lock: None)
    monkeypatch.setattr(arr_serve, "daily", lambda: None)
    arr_serve.main(["--worker"])
    arr_serve.main(["--daily"])
    assert capsys.readouterr().out == ""


def test_under_the_listener_the_summary_line_goes_to_stdout_too(app, monkeypatch, capsys):
    """The worker the listener starts runs as --serve --worker, so the container log carries the logfmt line. Syslog gets
    it as before, and a hook or a backfill prints nothing more."""
    sent, rec = [], dict(source="hook", app="radarr", outcome="no_change", label="Film A (1979)", id="x1")
    monkeypatch.setattr(syslog, "syslog", lambda priority, line: sent.append(line))
    monkeypatch.setattr(h, "SERVE", False)
    h.decision(rec, time.time())
    assert capsys.readouterr().out == "" and len(sent) == 1
    monkeypatch.setattr(h, "worker", lambda lock: h.decision(rec, time.time()))
    arr_serve.main(["--worker"])
    assert capsys.readouterr().out == sent[1] + "\n" and sent[1] == sent[0]
    assert sent[0].startswith("arr=radarr source=hook outcome=no_change ") and sent[0].endswith(' label="Film A (1979)" id=x1')
    monkeypatch.setattr(h, "SERVE", False)
    ran = []
    monkeypatch.setattr(h, "main", lambda argv: ran.append((argv, h.SERVE)))
    arr_serve.main(["--audit", "radarr", "--since", "24h", "--post"])   # the nightly audit, see daily()
    assert ran == [(["--audit", "radarr", "--since", "24h", "--post"], True)]


def test_under_the_listener_the_audit_writes_only_its_summary_line(app, monkeypatch, capsys):
    """The nightly audit runs as --serve --audit, see daily(). Its one summary line goes to syslog and to the container
    log. The terminal text stays out of the container log. The owner saw it there after each summary line of 2.1.0:
    Edit audit: Radarr docker
    0 files edited since 2026-10-01T11:26-04:00, 0 of them plan a further edit. ..."""
    sent = []
    monkeypatch.setattr(syslog, "syslog", lambda priority, line: sent.append(line))
    monkeypatch.setattr(h.os, "nice", lambda n: None)
    monkeypatch.setattr(h.subprocess, "run", lambda argv, **k: None)   # ionice
    monkeypatch.setattr(h, "SERVE", False)   # main() sets it, and the test ends with it unset
    arr_serve.main(["--audit", "radarr", "--since", "24h"])
    (line,) = sent
    assert line.startswith("arr=radarr source=audit outcome=summary edited=0 further=0 undecided=0 dropped=0 broken=0 tmdb=")
    assert capsys.readouterr().out == line + "\n"
    monkeypatch.setattr(h, "SERVE", False)
    h.main(["--audit", "radarr", "--since", "24h"])   # in a terminal, only the text
    assert capsys.readouterr().out.startswith(f"Audit check: 0 problems · Radarr {h.CFG.instance}\n0 files changed since ")


def test_spawn_starts_a_new_program_in_its_own_session(monkeypatch):
    seen = []
    monkeypatch.setattr(arr_serve.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    arr_serve.spawn("worker")
    (argv, kw), = seen
    assert argv[1:] == [os.path.realpath(h.__file__), "--serve", "--worker"] and kw == {"start_new_session": True}


def test_the_worker_and_daily_modes_run_their_part(app, monkeypatch):
    ran = []
    monkeypatch.setattr(h, "SERVE", False)   # main() sets it, and the test ends with it unset
    monkeypatch.setattr(h, "worker", lambda lock: ran.append(("worker", h.try_lock("worker.lock") is None)))
    monkeypatch.setattr(arr_serve, "daily", lambda: ran.append(("daily", None)))
    arr_serve.main(["--worker"])
    held = h.try_lock("worker.lock")
    arr_serve.main(["--worker"])   # another worker runs: this one exits
    held.close()
    arr_serve.main(["--daily"])
    assert ran == [("worker", True), ("daily", None)]
    with pytest.raises(SystemExit, match="usage"):
        arr_serve.main(["--other"])


@pytest.mark.parametrize("left", ["queued", "claimed", "plex", "deep", "file"])
def test_work_waits_for_a_queued_or_claimed_job_or_kept_plex_analyzes(app, left):
    """A queued, claimed or deep analysis job, the Plex analyzes a stopped worker kept, or a job file the hook wrote
    while the store was busy. Half a job file and a job put back for later are no work yet."""
    assert not h.waiting()
    os.makedirs(h.queue_dir())
    open(os.path.join(h.queue_dir(), ".1-1.json"), "w").close()   # half a job is no job
    h.store.write("INSERT INTO jobs (name, due, at, job) VALUES ('9-1.json', ?, 0, '{}')", time.time_ns() + 10**12)
    assert not h.waiting()
    if left == "file":
        open(os.path.join(h.queue_dir(), "1-1.json"), "w").close()
    elif left == "plex":
        h.store.put("plex", "pending", [{"path": "/x.mkv"}])
    else:
        h.store.write("INSERT INTO jobs (name, claimed, at, job) VALUES (?, ?, 0, '{}')", "deep-analysis-ab.json" if left == "deep" else "1-1.json",
                      int(left == "claimed"))
    assert h.waiting()


def test_the_daily_jobs_run_once_a_day_after_their_time(app):
    day = datetime.datetime(2026, 9, 30, 7, 29)
    assert not arr_serve.daily_due("07:30", day)
    assert arr_serve.daily_due("07:30", day.replace(minute=30))
    assert not arr_serve.daily_due("07:30", day.replace(hour=23))
    assert arr_serve.daily_due("07:30", day + datetime.timedelta(days=1, hours=5))   # a missed day runs at the next look
    assert not arr_serve.daily_due("", day + datetime.timedelta(days=3))


def test_the_daily_jobs_audit_each_app_with_a_key_then_rotate_the_log(app, monkeypatch, settings):
    runs = []
    settings(radarr={"api_key": "k"}, sonarr={"api_key": "", "dir": "/nonexistent"})
    monkeypatch.setattr(arr_serve.subprocess, "run", lambda argv, **kw: runs.append(argv))
    arr_serve.daily()
    assert [r[2:] for r in runs[:-1]] == [["--serve", "--audit", "radarr", "--since", "24h", "--post"]]   # its summary lines reach the container log
    state = os.path.join(h.CFG.state_dir, "logrotate.state")
    assert runs[-1][:3] == ["logrotate", "-s", state] and runs[-1][-1].endswith("logrotate.conf")   # the state stays in /config
    assert open(runs[-1][-1]).read().startswith(h.CFG.log + " {\n    weekly\n")


def test_the_daily_jobs_go_on_without_logrotate(app, monkeypatch, settings, capsys):
    settings(radarr={"api_key": "", "dir": "/nonexistent"}, sonarr={"dir": "/nonexistent"})
    def run(argv, **kw):
        if argv[0] == "logrotate":
            raise FileNotFoundError(2, "No such file or directory", "logrotate")
    monkeypatch.setattr(arr_serve.subprocess, "run", run)
    arr_serve.daily()
    assert "logrotate is not installed, so the decision log is not rotated" in capsys.readouterr().out


def test_an_app_whose_config_xml_holds_no_key_is_left_out(app, monkeypatch, settings, tmp_path):
    (tmp_path / "config.xml").write_text("<Config><Port>8989</Port></Config>")
    settings(sonarr={"dir": str(tmp_path), "api_key": ""}, radarr={"api_key": "k"})
    assert arr_serve.apps_on() == ["radarr"]


FAKE_WORKER = """
import os, signal, sqlite3, subprocess, sys, time
queue, marks, kid = sys.argv[1:]
mark = lambda text: open(marks, "a").write(text + "\\n")
with sqlite3.connect(queue) as db:   # the worker takes the jobs of the store
    db.execute("DELETE FROM jobs")
open(kid, "w").write(str(subprocess.Popen(["sleep", "30"]).pid))   # an ffmpeg of the job, in the worker's group
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
mark("started")
time.sleep(1.5)   # the flag edit
mark("edited")
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})   # the stop that waited ends the worker here
time.sleep(0.5)
mark("went on")
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_listener_queues_starts_a_worker_and_stops_on_sigterm(app, monkeypatch, settings, tmp_path):
    """The real main loop in a forked child: a Download post, the worker it starts, and a stop by SIGTERM. The worker is
    a program in its own session, as spawn() starts it. It blocks SIGTERM for a while, as no_stop() does during a flag
    edit, then keeps the default action, as worker() does. The listener must stop it and wait for it."""
    port, marks, kid = free_port(), tmp_path / "marks", tmp_path / "kid"
    settings(webhook_user="guard", webhook_password="s3cret-pass", audit_time="")
    monkeypatch.setattr(arr_serve, "PORT", port)
    monkeypatch.setattr(arr_serve, "POLL", 0.1)   # the listener sees the stop at once, while the worker holds its edit
    monkeypatch.setattr(arr_serve, "spawn", lambda what: subprocess.Popen(
        [sys.executable, "-c", FAKE_WORKER, h.store.path(), str(marks), str(kid)], start_new_session=True))
    out = tmp_path / "stdout"
    pid = h.store.fork()   # the test process holds a connection to the store, see store.fork()
    if not pid:
        try:
            sys.stdout = open(out, "w", buffering=1)   # the listener's lines, for the parent to read
            arr_serve.main([])
        finally:
            os._exit(0)
    try:
        for _ in range(100):   # the first pass of the loop finds no work
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", "/health")
                assert c.getresponse().status == 200
                break
            except ConnectionRefusedError:
                time.sleep(0.1)
        time.sleep(0.5)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)   # a job starts a worker at once, not at the next TICK
        c.request("POST", "/radarr", body=json.dumps(radarr_body(app)), headers={"Authorization": AUTH})
        assert c.getresponse().status == 200
        for _ in range(100):
            if marks.exists():
                break
            time.sleep(0.1)
        assert marks.read_text() == "started\n" and h.queued() == []
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/nothing")
        assert c.getresponse().status == 404
        for _ in range(50):   # the loop prints the summary of the refused requests
            if "refused 1 request without the right path or credentials" in out.read_text():
                break
            time.sleep(0.1)
        assert "refused 1 request without the right path or credentials" in out.read_text() and "/nothing" not in out.read_text()
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)   # the listener closed its socket, and the worker, still in its edit, holds no copy of it
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=5).close()
        end, status = time.monotonic() + 20, None
        while time.monotonic() < end and status is None:
            done, st = os.waitpid(pid, os.WNOHANG)
            status = st if done else None
            time.sleep(0.05)
        assert status == 0 and marks.read_text() == "started\nedited\n"   # it waited for the edit, and the stop ended the worker
        for _ in range(50):   # the stop reached the worker's whole group: its child ends too
            state = open(f"/proc/{kid.read_text()}/stat").read().split(")")[-1].split()[0] if os.path.exists(f"/proc/{kid.read_text()}") else "gone"
            if state in ("Z", "gone"):
                break
            time.sleep(0.1)
        assert state in ("Z", "gone"), state
    finally:
        with __import__("contextlib").suppress(ProcessLookupError, ChildProcessError):
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_the_listener_runs_the_daily_jobs_again_once_the_last_run_ended(app, monkeypatch, settings, tmp_path):
    """A daily run is a child of the listener. The listener reaps it, so the next day's run starts."""
    port, marks, due = free_port(), tmp_path / "daily", iter([True, True, True])
    settings(webhook_user="guard", webhook_password="s3cret-pass", audit_time="00:00")
    monkeypatch.setattr(arr_serve, "PORT", port)
    monkeypatch.setattr(arr_serve, "POLL", 0.05)
    monkeypatch.setattr(arr_serve, "TICK", 0.2)
    monkeypatch.setattr(arr_serve, "daily_due", lambda at, now=None: next(due, False))
    monkeypatch.setattr(arr_serve, "spawn", lambda what: subprocess.Popen([sys.executable, "-c", f"open({str(marks)!r}, 'a').write('run\\n')"]))
    pid = h.store.fork()   # the test process holds a connection to the store, see store.fork()
    if not pid:
        try:
            arr_serve.main([])
        finally:
            os._exit(0)
    try:
        for _ in range(60):
            if marks.exists() and marks.read_text().count("run") >= 2:
                break
            time.sleep(0.1)
        assert marks.read_text().count("run") >= 2
    finally:
        with __import__("contextlib").suppress(ProcessLookupError, ChildProcessError):
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)


@pytest.fixture
def live_srv(app, monkeypatch):
    """The real server with its pool, serving in a thread."""
    monkeypatch.setattr(arr_serve.Handler, "auth", AUTH.encode())
    srv = arr_serve.Server(("127.0.0.1", 0), arr_serve.Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    srv.pool.shutdown(wait=True)   # each handler ends here, so a late one never counts in the next test's REFUSALS
    t.join(20)


@pytest.fixture
def live(live_srv):
    """The port of live_srv."""
    return live_srv.server_address[1]


def timed_test_post(port):
    """(status, seconds) of a real Test post."""
    started = time.monotonic()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", "/sonarr", body=json.dumps({"eventType": "Test"}), headers={"Authorization": AUTH})
    status = c.getresponse().status
    c.close()
    return status, time.monotonic() - started


def test_a_trickling_client_is_cut_at_the_deadline_and_holds_up_no_app(live, monkeypatch):
    monkeypatch.setattr(arr_serve, "READ_TIMEOUT", 1.0)
    slow = socket.create_connection(("127.0.0.1", live), timeout=10)
    started, cut = time.monotonic(), None
    for b in b"POST /sonarr HTTP/1.1\r\nHost: x\r\nX-Pad: " + b"a" * 40:   # one byte every 0.1 s, never a whole request
        try:
            slow.sendall(bytes([b]))
        except OSError:
            cut = time.monotonic() - started
            break
        time.sleep(0.1)
        if b == ord("H"):
            assert timed_test_post(live) == (200, pytest.approx(0, abs=0.9))   # an app's post goes on meanwhile
    slow.settimeout(5)
    with contextlib.suppress(OSError):
        assert slow.recv(10) == b""
    cut = cut or time.monotonic() - started
    slow.close()
    assert 0.9 <= cut <= 3, cut
    for _ in range(50):   # the handler ends right after the cut
        if arr_serve.REFUSALS.n:
            break
        time.sleep(0.05)
    assert arr_serve.REFUSALS.n == 1 and not os.path.exists(h.CFG.log)


def test_idle_connections_leave_the_threads_to_an_apps_post(live, monkeypatch):
    idle = [socket.create_connection(("127.0.0.1", live), timeout=10) for _ in range(20)]   # the 20 of the live review
    time.sleep(0.2)
    status, took = timed_test_post(live)
    assert status == 200 and took < 1, took   # READ_TIMEOUT stays 10 s here, so nothing waited for an idle one
    for c in idle:
        c.close()


def test_more_idle_connections_than_threads_delay_a_post_at_most_the_deadline(live, monkeypatch):
    monkeypatch.setattr(arr_serve, "READ_TIMEOUT", 1.0)
    idle = [socket.create_connection(("127.0.0.1", live), timeout=10) for _ in range(arr_serve.THREADS + 8)]
    time.sleep(0.2)
    status, took = timed_test_post(live)
    assert status == 200 and took < 2.5, took
    for c in idle:
        c.close()


def test_a_connection_past_the_backlog_closes_at_once(app, monkeypatch):
    monkeypatch.setattr(arr_serve, "THREADS", 1)
    monkeypatch.setattr(arr_serve, "BACKLOG", 1)
    srv = arr_serve.Server(("127.0.0.1", 0), arr_serve.Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
    t.start()
    try:
        first = [socket.create_connection(srv.server_address, timeout=10) for _ in range(2)]   # one thread, one waiting
        time.sleep(0.2)
        third = socket.create_connection(srv.server_address, timeout=2)
        assert third.recv(10) == b"" and arr_serve.REFUSALS.n == 1
        for c in first + [third]:
            c.close()
    finally:
        srv.shutdown()
        srv.server_close()
        srv.pool.shutdown(wait=True)   # the idle connection's handler ends here, see live_srv()
        t.join(20)


def test_refused_posts_are_counted_and_summed_up_once_a_minute(server, app, capsys):
    for _ in range(30):
        assert server("POST", "/radarr", radarr_body(app), headers={"Authorization": "Basic d3Jvbmc6d3Jvbmc="})[0] == 401
        assert server("POST", "/lidarr", {})[0] == 404
    assert not os.path.exists(h.CFG.log) and "POST /radarr" not in capsys.readouterr().out   # no line per request
    assert arr_serve.REFUSALS.flush(now=1000.0) == "arr-media-guard: refused 60 requests without the right path or credentials, the last from 127.0.0.1"
    assert arr_serve.REFUSALS.flush(now=1001.0) is None   # nothing new
    server("POST", "/radarr", {}, headers={"Authorization": ""})
    assert arr_serve.REFUSALS.flush(now=1030.0) is None and arr_serve.REFUSALS.flush(now=1060.0).startswith("arr-media-guard: refused 1 request without")


def test_the_request_log_escapes_control_characters(capsys):
    hd = arr_serve.Handler.__new__(arr_serve.Handler)
    hd.client_address, hd.quiet = ("192.0.2.9", 1), False
    hd.log_message('"%s" %s', "POST /radarr\x1b[31m\x07\r\x00\x7f\x9b HTTP/1.1", "200")
    assert capsys.readouterr().out == 'arr-media-guard: 192.0.2.9 "POST /radarr\\x1b[31m\\x07\\x0d\\x00\\x7f\\x9b HTTP/1.1" 200\n'
    hd.quiet = True
    hd.log_message("%s", "anything")
    assert capsys.readouterr().out == ""


def test_a_body_that_nests_too_deep_is_refused(server, app):
    raw = b"[" * 200000 + b"]" * 200000
    code, text, _ = server("POST", "/radarr", raw=raw)
    assert code == 400 and "nests too deep" in text and log_lines()[0]["result"] == "refused"


def test_the_deadline_covers_the_request_and_never_the_apps_answer(live, app, monkeypatch):
    """The app's API may answer slowly. Only the request itself must arrive in READ_TIMEOUT."""
    monkeypatch.setattr(arr_serve, "READ_TIMEOUT", 0.5)
    fast = h.arr
    monkeypatch.setattr(h, "arr", lambda a, p: time.sleep(1.0) or fast(a, p))
    assert timed_test_post(live)[0] == 200


def test_one_job_file_is_written_at_a_time(server, app, monkeypatch):
    held = []
    real = h.queue_job
    monkeypatch.setattr(h, "queue_job", lambda job: held.append(arr_serve.WRITES.locked()) or real(job))
    assert server("POST", "/radarr", radarr_body(app))[0] == 200 and held == [True]


def test_a_client_that_went_away_leaves_no_traceback(app, monkeypatch):
    srv = arr_serve.Server(("127.0.0.1", 0), arr_serve.Handler)
    errors = []
    monkeypatch.setattr(srv, "handle_error", lambda request, address: errors.append(address))
    for ex in (BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset"), ValueError("a bug")):
        monkeypatch.setattr(srv, "finish_request", lambda request, address, ex=ex: (_ for _ in ()).throw(ex))
        srv.slots.acquire()
        srv.serve(socket.socket(), ("192.0.2.1", 1))
    assert errors == [("192.0.2.1", 1)]   # only the bug prints its traceback
    srv.server_close()


def test_every_slot_comes_back_after_its_request(live):
    """More requests one after another than the listener holds at once: each one ends and gives its slot back."""
    for _ in range(arr_serve.THREADS + arr_serve.BACKLOG + 10):
        c = http.client.HTTPConnection("127.0.0.1", live, timeout=10)
        c.request("GET", "/health")
        assert c.getresponse().status == 200
        c.close()


def test_the_listener_holds_96_connections_and_closes_the_97th(live, live_srv, monkeypatch):
    monkeypatch.setattr(arr_serve, "READ_TIMEOUT", 1.0)
    assert (arr_serve.THREADS, arr_serve.BACKLOG, arr_serve.Server.request_queue_size) == (32, 64, 64)
    idle = [socket.create_connection(("127.0.0.1", live), timeout=10) for _ in range(95)]
    time.sleep(0.3)
    status, took = timed_test_post(live)   # the 96th waits for a thread, at most until the idle ones expire
    assert status == 200 and took < 5, took
    for c in idle:
        c.close()
    settled(live_srv, arr_serve.THREADS + arr_serve.BACKLOG)
    monkeypatch.setattr(arr_serve, "READ_TIMEOUT", 30.0)   # the held ones stay open while the test looks
    before = arr_serve.REFUSALS.n
    conns = [socket.create_connection(("127.0.0.1", live), timeout=10) for _ in range(97)]
    # A loaded host may accept them out of order, so any one of the 97 may be the one that closes.
    ready = select.select(conns, [], [], 20)[0]
    assert len(ready) == 1 and ready[0].recv(10) == b"" and arr_serve.REFUSALS.n == before + 1
    assert select.select(conns, [], [], 0.5)[0] == ready   # the other 96 stay open
    for c in conns:
        c.close()


LISTENER = """
import sys
sys.path.insert(0, sys.argv[1])
from arr_media_guard import serve
serve.PORT, serve.POLL, serve.START_WAIT = int(sys.argv[2]), 0.1, 1
serve.main([])
"""


def test_a_stop_ends_the_listener_at_once_and_prints_the_last_count(tmp_path):
    """The real interpreter exit, which waits for the pool's threads. An idle connection must not hold it up until its
    deadline, so Docker never has to kill the listener. The refusals since the last summary line print at the stop."""
    port, env = free_port(), tmp_path / "env"
    env.write_text(f"WEBHOOK_USER='guard'\nWEBHOOK_PASSWORD='s3cret-pass'\nAUDIT_TIME=''\nSTATE_DIR='{tmp_path}'\nLOG='{tmp_path}/log.jsonl'\n"
                   f"POLICY_FILE='{os.path.abspath(os.path.join(FILES, 'examples', 'policy.json'))}'\nRADARR_DIR='/nonexistent'\nSONARR_DIR='/nonexistent'\n")
    p = subprocess.Popen([sys.executable, "-c", LISTENER, os.path.abspath(FILES), str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=dict(os.environ, ARR_MEDIA_GUARD_ENV=str(env)))
    try:
        for _ in range(100):
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/nothing")   # refused: the first summary line prints at once
                assert c.getresponse().status == 404
                break
            except ConnectionRefusedError:
                time.sleep(0.1)
        time.sleep(0.3)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/nothing")   # counted, and printed only at the stop
        assert c.getresponse().status == 404
        idle = socket.create_connection(("127.0.0.1", port), timeout=5)
        time.sleep(0.3)
        started = time.monotonic()
        p.send_signal(signal.SIGTERM)
        out = p.communicate(timeout=20)[0]
        assert p.returncode == 0 and time.monotonic() - started < 2, (time.monotonic() - started, out)
        assert out.count("arr-media-guard: refused ") == 2 and out.rstrip().endswith("arr-media-guard: stopped"), out
        idle.close()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_the_listener_prints_each_map_and_checks_the_paths_at_its_start(tmp_path):
    """The start line names the map of each program. The checks run beside the listener, so an app that does not
    answer gives one warning, from the start check, and never holds up the start. A bad pair in any map stops the start."""
    port, env, out = free_port(), tmp_path / "env", tmp_path / "out"
    base = (f"WEBHOOK_USER='guard'\nWEBHOOK_PASSWORD='s3cret-pass'\nAUDIT_TIME=''\nSTATE_DIR='{tmp_path}'\nLOG='{tmp_path}/log.jsonl'\n"
            f"POLICY_FILE='{os.path.abspath(os.path.join(FILES, 'examples', 'policy.json'))}'\nRADARR_DIR='/nonexistent'\n"
            f"SONARR_API_KEY='0123abcd'\nSONARR_URL='http://127.0.0.1:{free_port()}'\nPATH_MAP='/data:/media'\n")
    env.write_text(base + "PLEX_PATH_MAP='/mnt/TV Shows:/media/TV'\n")
    with open(out, "w") as f:
        p = subprocess.Popen([sys.executable, "-c", LISTENER, os.path.abspath(FILES), str(port)], stdout=f, stderr=subprocess.STDOUT,
                             env=dict(os.environ, ARR_MEDIA_GUARD_ENV=str(env)))
    try:
        for _ in range(100):
            if "sonarr warning: the start check failed" in out.read_text():
                break
            time.sleep(0.1)
        text = out.read_text()
        assert text.startswith(arr_serve.BANNER + "\narr-media-guard ") and text.count(arr_serve.BANNER) == 1, text   # once, above the listening line
        assert "Path maps: sonarr /data:/media, radarr /data:/media, plex /mnt/TV Shows:/media/TV." in text, text
        assert "arr-media-guard: sonarr warning: the start check failed: the Sonarr API did not answer: URLError" in text, text
        assert "Sonarr did not answer, so its root folders are not checked" not in text, text   # the path check leaves it to the start check
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
    env.write_text(base + "RADARR_PATH_MAP='/movies:/media/movies|movies:/m'\n")
    r = subprocess.run([sys.executable, "-c", LISTENER, os.path.abspath(FILES), str(port)], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, ARR_MEDIA_GUARD_ENV=str(env)))
    assert r.returncode == 1 and "--serve: RADARR_PATH_MAP takes pairs APP_PATH:LOCAL_PATH" in r.stderr, r.stderr


def image_env():
    """The env file of the image, as the Dockerfile writes it."""
    return subprocess.run([sys.executable, os.path.join(FILES, "docker", "merge_env.py")], capture_output=True, text=True, check=True).stdout


def test_a_fresh_env_file_gets_a_password_and_the_listener_takes_it(tmp_path):
    """The image's env file as the first start writes it, with the paths of this test. The listener generates the
    password, starts and takes the pair from the file. Its output never shows the password."""
    port, env, out = free_port(), tmp_path / "arr-media-guard.env", tmp_path / "out"
    env.write_text(image_env() + f"AUDIT_TIME=''\nSTATE_DIR='{tmp_path}'\nLOG='{tmp_path}/log.jsonl'\nRADARR_DIR='/nonexistent'\nSONARR_DIR='/nonexistent'\n"
                   f"POLICY_FILE='{os.path.abspath(os.path.join(FILES, 'examples', 'policy.json'))}'\n")
    with open(out, "w") as f:
        p = subprocess.Popen([sys.executable, "-c", LISTENER, os.path.abspath(FILES), str(port)], stdout=f, stderr=subprocess.STDOUT,
                             env=dict(os.environ, ARR_MEDIA_GUARD_ENV=str(env)))
    try:
        for _ in range(100):
            if "listening on port" in out.read_text():
                break
            time.sleep(0.1)
        user, pw = h.env_file(str(env))["WEBHOOK_USER"], h.env_file(str(env))["WEBHOOK_PASSWORD"]
        assert user == "arr-admin" and len(pw) == 32, out.read_text()
        for secret, code in ((pw, 200), (pw[:-1], 401)):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/radarr", b'{"eventType": "Other"}', {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()})
            assert c.getresponse().status == code
            c.close()
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
    text = out.read_text()
    assert text.count("arr-media-guard: generated a Webhook password") == 1 and pw not in text, text


def test_each_start_writes_the_example_and_only_the_first_start_the_env_file(tmp_path, monkeypatch):
    """The start script as root, with its paths in tmp_path and id, chown and setpriv as stubs. The first start writes
    the image's env file as the env file and as the example, both 0640. A later start writes the example again and
    leaves the env file alone, so the generated password stays in the env file only."""
    opt, cfg, stubs = tmp_path / "opt", tmp_path / "config", tmp_path / "bin"
    for d in (opt / "docker", opt / "examples", stubs):
        d.mkdir(parents=True)
    image = image_env()
    (opt / "docker" / "arr-media-guard.env.example").write_text(image)
    (opt / "examples" / "policy.json").write_text("{}")
    for path, body in ((opt / "arr-media-guard", "exit 0"), (stubs / "id", "echo 0"), (stubs / "chown", "exit 0"),
                       (stubs / "setpriv", 'shift 3\nexec "$@"')):
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
    script = tmp_path / "start.sh"
    with open(os.path.join(FILES, "docker", "arr-media-guard.sh")) as f:
        script.write_text(f.read().replace("/opt/arr-media-guard", str(opt)).replace("/config", str(cfg)))
    run = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", PUID="1000", PGID="1000")
    subprocess.run(["sh", str(script), "--serve"], check=True, env=run)
    env, example = cfg / "arr-media-guard.env", cfg / "arr-media-guard.env.example"
    assert env.read_text() == example.read_text() == image
    assert {oct(p.stat().st_mode & 0o7777) for p in (env, example)} == {oct(0o640)}
    monkeypatch.setattr(arr_serve.config, "ENV_FILE", str(env))
    pw = arr_serve.new_password("arr-admin")[1]
    mine = env.read_text()
    example.write_text("# the example of an older image\n")
    subprocess.run(["sh", str(script), "--serve"], check=True, env=run)
    assert env.read_text() == mine and f"WEBHOOK_PASSWORD='{pw}'" in mine
    assert example.read_text() == image and pw not in image


def test_a_missing_recycle_bin_warns_in_the_test_and_the_selftest_and_fails_neither(server, app, monkeypatch, settings, capsys):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}
    code, text, _ = server("POST", "/radarr", {"eventType": "Test"})
    assert code == 200 and "Warning: Radarr has no recycle bin, so the restore after a bad upgrade cannot work." in text
    assert "arr-media-guard: radarr warning: Radarr has no recycle bin" in capsys.readouterr().out
    settings(radarr={"api_key": "k"}, sonarr={"dir": "/nonexistent"})   # an app this host does not run: no warning, no failure
    h.main(["--selftest"])
    out = capsys.readouterr().out
    assert "warning: Radarr has no recycle bin" in out and "Sonarr" not in out and out.rstrip().endswith("selftest ok")


def test_a_config_xml_with_no_key_gives_no_warning_in_the_selftest(app, monkeypatch, settings, capsys, tmp_path):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}   # a bin warning for any app the selftest asks
    (tmp_path / "config.xml").write_text("<Config><Port>8989</Port></Config>")
    settings(sonarr={"dir": str(tmp_path), "api_key": ""}, radarr={"dir": "/nonexistent", "api_key": ""})
    h.main(["--selftest"])
    out = capsys.readouterr().out
    assert "warning:" not in out and out.rstrip().endswith("selftest ok") and app["calls"] == []


def test_the_custom_script_test_prints_the_bin_warning(app, monkeypatch, capsys):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}
    for k in list(os.environ):
        if k.startswith(("radarr_", "sonarr_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("radarr_eventtype", "Grab")   # only Test asks for the bin
    h.hook()
    assert capsys.readouterr().out == "arr-media-guard: Grab ok\n" and app["calls"] == []
    monkeypatch.setenv("radarr_eventtype", "Test")
    h.hook()
    out = capsys.readouterr().out
    assert out.startswith("arr-media-guard: warning: Radarr has no recycle bin") and out.endswith("arr-media-guard: Test ok\n")


def test_the_custom_script_test_fails_when_the_api_or_a_root_folder_fails(app, monkeypatch, capsys):
    """The exit code fails the app's Test, as the listener's 500 does. A broken policy fails it too, see
    test_a_test_event_fails_when_no_policy_loaded."""
    env_event(monkeypatch, {"radarr_eventtype": "Test"})
    app["api"]["rootfolder"] = urllib.error.URLError("refused")
    with pytest.raises(SystemExit) as ex:
        h.hook()
    assert str(ex.value) == "arr-media-guard: the Radarr API did not answer: URLError: <urlopen error refused>"
    app["api"]["rootfolder"] = [{"path": app["movies"]}, {"path": "/nonexistent/movies"}]
    with pytest.raises(SystemExit) as ex:
        h.hook()
    assert str(ex.value) == ("arr-media-guard: this script does not see the root folders /nonexistent/movies. Mount the media at the app's "
                             "paths, or set RADARR_PATH_MAP or PATH_MAP")
    assert "Test ok" not in capsys.readouterr().out


@pytest.mark.parametrize("path", ["config/mediamanagement", "rootfolder"])
def test_an_app_that_does_not_answer_gives_no_bin_warning(app, path):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}
    app["api"][path] = urllib.error.URLError("refused")
    assert h.bin_warnings("radarr") == []


def test_a_recycle_bin_on_another_file_system_warns(app, monkeypatch):
    real = h.volume
    monkeypatch.setattr(h, "volume", lambda p: -1 if p.startswith(app["rbin"]) else real(p))
    (w,) = h.bin_warnings("radarr")
    assert w.startswith(f"Radarr's recycle bin {app['rbin']} is on another file system than ") and "needs a rename" in w


def test_a_recycle_bin_on_its_own_bind_mount_of_the_same_file_system_warns(app, monkeypatch):
    """The bin and the root folders share st_dev, but the bin is its own mount, so a rename from it fails with EXDEV."""
    real = os.path.ismount
    monkeypatch.setattr(h.os.path, "ismount", lambda p: p == app["rbin"] or real(p))
    (w,) = h.bin_warnings("radarr")
    assert w.startswith(f"Radarr's recycle bin {app['rbin']} is on another file system than ") and "needs a rename" in w


def test_a_recycle_bin_beside_the_media_gives_no_warning(app):
    assert h.bin_warnings("sonarr") == [] and h.bin_warnings("radarr") == []


# --- KEEP_REPLACED: the Grab event and its warnings ---------------------------------------------------------------

GRAB_ON = [{"name": "guard", "implementation": "Webhook", "onGrab": True, "fields": [{"name": "url", "value": f"http://x/{a}"}]}
           for a in ("radarr", "sonarr")]   # a saved connection of each app that sends Grab


def keep_on(monkeypatch, tmp_path):
    """KEEP_REPLACED on, with the top of the mount at tmp_path. Returns replaced_root()."""
    monkeypatch.setattr(h, "CFG", dataclasses.replace(h.CFG, keep_replaced=True))
    monkeypatch.setattr(h, "mount_top", lambda f: str(tmp_path))
    return str(tmp_path / h.CFG.recycle_dir)


def test_a_grab_post_links_the_files_the_grab_may_replace(server, app, monkeypatch, tmp_path, capsys):
    """Radarr names the movie, Sonarr the episodes, and the API gives their files. A two-episode file is linked once. The
    line on stdout names a file that the API lists and that is not on disk."""
    keep_on(monkeypatch, tmp_path)
    app["api"]["movie/7"]["movieFile"] = {"id": 31, "path": app["film"]}
    app["api"]["episode?episodeIds=901&episodeIds=902"] = [{"id": 901, "episodeFileId": 41, "seriesId": 5}, {"id": 902, "episodeFileId": 41, "seriesId": 5}]
    code, text, _ = server("POST", "/radarr", {"eventType": "Grab", "movie": {"id": 7}, "downloadId": "SABnzbd_nzo_abc123"})
    assert (code, text) == (200, "arr-media-guard: Grab ok\n")
    code, text, _ = server("POST", "/sonarr", {"eventType": "Grab", "series": {"id": 5}, "episodes": [{"id": 901}, {"id": 902}], "downloadId": "D1"})
    assert (code, text) == (200, "arr-media-guard: Grab ok\n")
    recs = h.kept_read()
    assert sorted((r["app"], r["old"], r["download_id"]) for r in recs) == [("radarr", app["film"], "SABnzbd_nzo_abc123"), ("sonarr", app["ep"], "D1")]
    assert all(os.stat(r["kept"]).st_ino == os.stat(r["old"]).st_ino for r in recs) and h.queued() == []
    assert "arr-media-guard: radarr grab: kept 1 file.\n" in capsys.readouterr().out
    app["api"]["movie/7"]["movieFile"] = {"id": 31, "path": app["film"] + ".gone"}
    server("POST", "/radarr", {"eventType": "Grab", "movie": {"id": 7}, "downloadId": "D2"})
    assert f"arr-media-guard: radarr grab: kept 0 files. Not kept: {app['film']}.gone: the file is not on disk\n" in capsys.readouterr().out


@pytest.mark.parametrize("path, body", [("/radarr", {"eventType": "Grab"}), ("/radarr", {"eventType": "Grab", "movie": {"id": 7}, "downloadId": 5}),
                                        ("/sonarr", {"eventType": "Grab", "series": {"id": 5}, "episodes": [{"id": "901"}]}),
                                        ("/radarr", {"eventType": "Grab", "movie": {"id": 7}, "downloadId": "x" * 201}),
                                        ("/radarr", {"eventType": "Grab", "movie": {"id": 8}})])
def test_a_grab_that_fails_answers_ok_and_logs_why(server, app, monkeypatch, tmp_path, path, body):
    """A body the hook cannot read never reaches the API. An API that fails logs too. The app's grab never fails on the
    hook."""
    keep_on(monkeypatch, tmp_path)
    app["api"]["movie/8"] = urllib.error.URLError("refused")
    code, text, _ = server("POST", path, body)
    assert (code, text) == (200, "arr-media-guard: Grab ok\n") and h.kept_read() == []
    assert app["calls"] == ([("radarr", "movie/8")] if body.get("movie") == {"id": 8} else [])
    (line,) = log_lines()
    assert (line["source"], line["result"]) == ("webhook", "error") and line["note"].startswith("the grab kept nothing: ")
    assert "hook" not in line["note"]


def test_keep_replaced_warns_when_the_connection_to_the_hook_sends_no_grab(app, monkeypatch, tmp_path):
    """The connection to this hook is a Custom Script with this script's path, or a Webhook to /radarr. Before the first
    Save the API lists none, and that warns too, because then a grab keeps nothing."""
    keep_on(monkeypatch, tmp_path)
    script = {"name": "guard", "implementation": "CustomScript", "onGrab": False, "fields": [{"name": "path", "value": h.__file__}]}
    web = {"name": "guard-web", "implementation": "Webhook", "onGrab": False, "fields": [{"name": "url", "value": "http://arr-media-guard:8484/radarr/"}]}
    other = {"name": "Discord", "implementation": "Discord", "onGrab": False, "fields": [{"name": "webHookUrl", "value": "https://x.invalid/radarr"}]}
    app["api"]["notification"] = [script, other]
    assert h.bin_warnings("radarr") == ["KEEP_REPLACED is on, but Radarr's connection guard does not send Grab, so arr-media-guard keeps nothing. "
                                        "Turn on On Grab in that connection."]
    script["onGrab"] = True
    assert h.bin_warnings("radarr") == []
    app["api"]["notification"] = [web, other]
    assert "Radarr's connection guard-web does not send Grab" in h.bin_warnings("radarr")[0]
    app["api"]["notification"] = [other]
    assert h.bin_warnings("radarr") == ["KEEP_REPLACED is on, but Radarr has no saved connection to arr-media-guard, so arr-media-guard "
                                        "keeps nothing at a grab. Save the connection with On Grab on."]
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".link-probe-")]   # the probe left nothing


def test_keep_replaced_warns_on_a_mount_that_takes_no_hard_link(app, monkeypatch, tmp_path):
    keep_on(monkeypatch, tmp_path)
    app["api"]["notification"] = GRAB_ON
    monkeypatch.setattr(h.os, "link", lambda a, b: (_ for _ in ()).throw(OSError(h.errno.EPERM, "Operation not permitted")))
    assert h.bin_warnings("radarr") == [f"KEEP_REPLACED is on, but {tmp_path} takes no hard link (Operation not permitted), so arr-media-guard keeps "
                                        "nothing on that mount. Keep the media on a file system that takes hard links."]
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".link-probe-")]


def test_keep_replaced_says_where_the_grab_links_go_when_the_mount_top_is_not_writable(app, monkeypatch, tmp_path):
    """The hook keeps them in a folder below the mount top. One at a root folder's top level shows in Library Import. The
    copies work, so a recycle bin on another file system gives no warning after these lines."""
    keep_on(monkeypatch, tmp_path)
    app["api"]["notification"] = GRAB_ON
    real_volume = h.volume
    monkeypatch.setattr(h, "volume", lambda p: -1 if p.startswith(app["rbin"]) else real_volume(p))
    real = os.access
    monkeypatch.setattr(h.os, "access", lambda p, mode, **k: False if str(p) == str(tmp_path) and mode & os.W_OK else real(p, mode, **k))
    movies, tv = os.path.join(str(tmp_path), "movies"), os.path.join(str(tmp_path), "tv")
    assert h.bin_warnings("sonarr") == [
        f"uid {os.getuid()} and gid {os.getgid()} cannot write in {tmp_path}, so arr-media-guard keeps its copies for {r} in {r}/{h.CFG.recycle_dir}. "
        "Sonarr's Library Import lists that folder as unmapped. Do not import it." for r in (movies, tv)]
    assert not [n for r in (movies, tv) for n in os.listdir(r) if n.startswith(".link-probe-")]


@pytest.mark.parametrize("made", [False, True])
def test_keep_replaced_names_the_folder_and_the_user_that_cannot_write_it(app, monkeypatch, tmp_path, made):
    """The probe writes in the folder, or in the mount top while the folder does not exist. The warning names the folder
    it probed, the uid and the gid, and what to do."""
    root = keep_on(monkeypatch, tmp_path)
    if made:
        os.mkdir(root)
    app["api"]["notification"] = GRAB_ON
    monkeypatch.setattr(h.tempfile, "mkstemp", lambda **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    where, fix = (root, f"Give {root} to them.") if made else (tmp_path, f"Create {root} and give it to them.")
    assert h.bin_warnings("radarr") == [f"KEEP_REPLACED is on, but uid {os.getuid()} and gid {os.getgid()} cannot write in {where} (Permission "
                                        f"denied), so arr-media-guard keeps nothing on that mount. {fix} In Docker, PUID and PGID set them."]


def test_the_link_probe_removes_both_files_when_it_stops_between_them(tmp_path, monkeypatch):
    """A time limit can stop the probe after the link and before its removal. Neither file stays."""
    real = os.link
    def link(a, b):
        real(a, b)
        raise SystemExit(142)
    monkeypatch.setattr(h.os, "link", link)
    with pytest.raises(SystemExit):
        h.link_probe(str(tmp_path / "recycle"))
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("serve, who", [(False, "script"), (True, "container")])
def test_a_recycle_bin_this_program_does_not_see_warns(app, monkeypatch, serve, who):
    monkeypatch.setattr(h, "SERVE", serve)
    app["api"]["config/mediamanagement"] = {"recycleBin": "/nonexistent/recycle"}
    assert h.bin_warnings("radarr") == [f"this {who} does not see Radarr's recycle bin /nonexistent/recycle, so the restore after a bad "
                                        "upgrade cannot use it. Mount it at that path, or set RADARR_PATH_MAP or PATH_MAP."]


@pytest.mark.parametrize("case", ["no bin", "not here", "other file system"])
def test_with_keep_replaced_working_the_recycle_bin_gives_no_warning(app, monkeypatch, settings, tmp_path, case):
    """The program keeps its own copy of each file an upgrade replaces, so the bin warning has nothing to act on. The
    owner saw this line at the first start of 2.1.0 in Docker:
    arr-media-guard: radarr warning: Radarr's recycle bin /media-storage/v2_media/.recycle/radarr is on another file
    system than /media-storage/all/movies. The hook keeps its own copy of each file an upgrade replaces, ..."""
    keep_on(monkeypatch, tmp_path)
    app["api"]["notification"] = [{"name": "guard", "implementation": "Webhook", "onGrab": True, "fields": [{"name": "url", "value": "http://x/radarr"}]}]
    if case == "other file system":
        real = h.volume
        monkeypatch.setattr(h, "volume", lambda p: -1 if p.startswith(app["rbin"]) else real(p))
    else:
        app["api"]["config/mediamanagement"] = {"recycleBin": "" if case == "no bin" else "/nonexistent/recycle"}
    assert h.bin_warnings("radarr") == []
    settings(keep_days=0)   # keeping nothing, the bin warning comes back
    w0, w1 = h.bin_warnings("radarr")
    assert w0 == "KEEP_REPLACED is on, but KEEP_ORIGINALS_DAYS is 0, so arr-media-guard keeps nothing at a grab. Set KEEP_ORIGINALS_DAYS above 0."
    assert ("cannot" in w1 or "needs a rename" in w1) and "hook" not in w1


def test_the_test_event_and_the_selftest_print_the_keep_warnings(server, app, monkeypatch, settings, tmp_path, capsys):
    keep_on(monkeypatch, tmp_path)
    app["api"]["notification"] = [{"name": "guard", "implementation": "Webhook", "onGrab": False, "fields": [{"name": "url", "value": "http://x/radarr"}]}]
    code, text, _ = server("POST", "/radarr", {"eventType": "Test"})
    assert code == 200 and "Warning: KEEP_REPLACED is on, but Radarr's connection guard does not send Grab" in text
    settings(radarr={"api_key": "k"}, sonarr={"dir": "/nonexistent"})
    capsys.readouterr()
    h.main(["--selftest"])
    out = capsys.readouterr().out
    assert "warning: KEEP_REPLACED is on, but Radarr's connection guard does not send Grab" in out and out.rstrip().endswith("selftest ok")
