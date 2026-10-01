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
"""Unit tests for arr_serve.py, the Webhook listener, and for PATH_MAP and the API key in the hook script.

No network beyond 127.0.0.1. The hook script is loaded by path, as in test_arr_media_guard.py, and the listener gets
it as its host module. The app API is a fake. The HTTP tests run the real handler on a free local port. One test forks
the real listener and stops it with SIGTERM.

Run: pytest tests/test_arr_serve.py
"""
import base64
import contextlib
import datetime
import http.client
import importlib.machinery
import importlib.util
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
os.environ["ARR_MEDIA_GUARD_LIB"] = FILES
os.environ["ARR_MEDIA_GUARD_ENV"] = "/nonexistent/arr-media-guard.env"
_loader = importlib.machinery.SourceFileLoader("arr_media_guard_serve", os.path.join(FILES, "arr-media-guard"))
h = importlib.util.module_from_spec(importlib.util.spec_from_loader("arr_media_guard_serve", _loader))
_loader.exec_module(h)
import arr_serve  # noqa: E402

with open(os.path.join(FILES, "examples", "policy.json")) as _f:
    h.arr_decide.set_policy(json.load(_f))
AUTH = "Basic " + base64.b64encode(b"guard:s3cret-pass").decode()


def http_error(code):
    return urllib.error.HTTPError("http://app.invalid", code, "error", {}, None)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A fake Radarr and Sonarr with one film and one episode file on disk, an upgrade's old files and a recycle bin.
    api maps an API path to its answer, or to an exception it raises. Returns the fake's state."""
    movies, tv, rbin = tmp_path / "movies" / "Film A (1979)", tmp_path / "tv" / "Show A" / "Season 1", tmp_path / "recycle"
    for d in (movies, tv, rbin):
        d.mkdir(parents=True)
    film, ep = movies / "Film A (1979) WEBDL-1080p.mkv", tv / "Show A - S01E02 - Two WEBDL-1080p.mkv"
    film.write_bytes(b"x")
    ep.write_bytes(b"x")
    state = tmp_path / "state"
    for d in ("queue", "claimed", "alerts"):
        (state / d).mkdir(parents=True)
    monkeypatch.setitem(h.CFG, "STATE_DIR", str(state))
    monkeypatch.setitem(h.CFG, "LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(h, "PATH_MAP", [])
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
        return h.mapped(v)   # the real arr() maps every answer
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
    job = json.load(open(os.path.join(h.queue_dir(), name)))
    os.remove(os.path.join(h.queue_dir(), name))
    return job


def log_lines():
    with open(h.CFG["LOG"]) as f:
        return [json.loads(line) for line in f]


# --- the Webhook body gives the job of the Custom Script variables -------------------------------------------------

def test_a_radarr_upgrade_body_gives_the_job_hook_writes(app, monkeypatch):
    old, copy = os.path.join(app["movies"], "Film A (1979) HDTV-720p.mkv"), os.path.join(app["rbin"], "Film A (1979)", "Film A (1979) HDTV-720p.mkv")
    old2 = os.path.join(app["movies"], "Film A (1979) HDTV-720p.en.srt")
    want = hook_job(monkeypatch, {"radarr_eventtype": "Download", "radarr_movie_id": "7", "radarr_moviefile_id": "31",
                                  "radarr_moviefile_path": app["film"], "radarr_moviefile_scenename": "Film.A.1979.1080p.WEB-DL-GRP",
                                  "radarr_download_id": "SABnzbd_nzo_abc123", "radarr_deletedpaths": f"{old}|{old2}",
                                  "radarr_deletedrecyclebinpaths": f"{copy}|"})
    got = arr_serve.job_of(h, "radarr", radarr_body(app, isUpgrade=True, deletedFiles=[
        {"id": 30, "path": old, "recycleBinPath": copy}, {"id": 29, "path": old2, "recycleBinPath": None}]))
    assert {k: v for k, v in got.items() if k != "time"} == {k: v for k, v in want.items() if k != "time"}
    assert list(got) == list(want)


def test_a_sonarr_body_gives_the_job_hook_writes_with_the_apis_episode_ids(app, monkeypatch):
    want = hook_job(monkeypatch, {"sonarr_eventtype": "Download", "sonarr_series_id": "5", "sonarr_episodefile_id": "41",
                                  "sonarr_episodefile_path": app["ep"], "sonarr_episodefile_episodeids": "901,902",
                                  "sonarr_episodefile_scenename": "", "sonarr_download_id": ""})
    got = arr_serve.job_of(h, "sonarr", sonarr_body(app, episodes=[{"id": 1}]))   # the posted episodes are never used
    assert {k: v for k, v in got.items() if k != "time"} == {k: v for k, v in want.items() if k != "time"}
    assert got["episode_ids"] == "901,902" and got["deleted"] is None and got["recycled"] is None


def test_the_job_takes_the_apis_path_and_scene_name_and_logs_the_posted_path(app):
    other = os.path.join(app["movies"], "elsewhere.mkv")
    job = arr_serve.job_of(h, "radarr", radarr_body(app, movieFile={"id": 31, "path": other, "sceneName": "Other.Name-GRP"}))
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
    ({}, {"moviefile/31": {"id": 31, "movieId": 7, "path": "/nonexistent/Film A.mkv"}}, 500, "does not see it"),
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
    with pytest.raises(arr_serve.Refused) as ex:
        arr_serve.job_of(h, "radarr", radarr_body(app, **body))
    assert ex.value.code == code and why in str(ex.value)


def test_an_upgrade_maps_the_old_paths_and_the_bin_paths_of_the_app(app, monkeypatch):
    """The app names its own paths in the body and in the API. The job holds the local ones, and the checks compare
    local with local."""
    monkeypatch.setattr(h, "PATH_MAP", [("/app/movies", os.path.dirname(app["movies"])), ("/app/recycle", app["rbin"])])
    rel = os.path.relpath(app["film"], os.path.dirname(app["movies"]))
    app["api"].update({"moviefile/31": {"id": 31, "movieId": 7, "path": f"/app/movies/{rel}", "sceneName": "Film.A-GRP"},
                       "movie/7": {"id": 7, "path": "/app/movies/Film A (1979)"}, "config/mediamanagement": {"recycleBin": "/app/recycle"}})
    body = radarr_body(app, movieFile={"id": 31, "path": f"/app/movies/{rel}"},
                       deletedFiles=[{"path": "/app/movies/Film A (1979)/old.mkv", "recycleBinPath": "/app/recycle/Film A (1979)/old.mkv"}])
    job = arr_serve.job_of(h, "radarr", body)
    assert job["path"] == app["film"] and not os.path.exists(h.CFG["LOG"])   # the mapped body path is the API's: no warning
    assert (job["deleted"], job["recycled"]) == (os.path.join(app["movies"], "old.mkv"), os.path.join(app["rbin"], "Film A (1979)", "old.mkv"))


def test_an_upgrade_without_a_recycle_bin_keeps_empty_bin_paths(app):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}
    old = os.path.join(app["movies"], "old.mkv")
    job = arr_serve.job_of(h, "radarr", radarr_body(app, deletedFiles=[{"path": old, "recycleBinPath": None}]))
    assert (job["deleted"], job["recycled"]) == (old, "")
    with pytest.raises(arr_serve.Refused):   # with no bin, no recycle bin copy can pass
        arr_serve.job_of(h, "radarr", radarr_body(app, deletedFiles=[{"path": old, "recycleBinPath": app["rbin"] + "/old.mkv"}]))


def test_an_import_complete_event_is_refused_with_what_to_change(app):
    with pytest.raises(arr_serve.Refused) as ex:
        arr_serve.job_of(h, "sonarr", sonarr_body(app, episodeFile=None, episodeFiles=[{"id": 41}]))
    assert ex.value.code == 400 and "On File Import" in str(ex.value)


def test_an_api_error_other_than_404_passes_up(app):
    app["api"]["moviefile/31"] = http_error(500)
    with pytest.raises(urllib.error.HTTPError):
        arr_serve.job_of(h, "radarr", radarr_body(app))


# --- PATH_MAP and the API key ---------------------------------------------------------------------------------------

def test_path_map_maps_both_ways_by_the_longest_whole_prefix(monkeypatch):
    monkeypatch.setattr(h, "PATH_MAP", [("/tv", "/media/tv"), ("/tv/kids", "/kids"), ("/", "/host")])
    assert h.mapped("/tv/Show/a.mkv") == "/media/tv/Show/a.mkv"
    assert h.mapped("/tv/kids/Show/a.mkv") == "/kids/Show/a.mkv"
    assert h.mapped("/tvshows/a.mkv") == "/host/tvshows/a.mkv"   # /tv matches whole folder names only
    assert h.mapped("/tv") == "/media/tv"
    assert h.mapped("/kids/Show/a.mkv", back=True) == "/tv/kids/Show/a.mkv"
    assert h.mapped("/media/tv/a.mkv", back=True) == "/tv/a.mkv"
    assert h.mapped({"a": ["/tv/x", 3, None, "Film /tv"], "b": {"c": "/tv/y"}}) == {"a": ["/media/tv/x", 3, None, "Film /tv"], "b": {"c": "/media/tv/y"}}
    monkeypatch.setattr(h, "PATH_MAP", [])
    assert h.mapped("/tv/a.mkv") == "/tv/a.mkv"


def test_a_map_to_the_root_and_a_blank_query_value_keep_their_shape(monkeypatch):
    monkeypatch.setattr(h, "PATH_MAP", [("/tv", "/")])
    assert (h.mapped("/tv"), h.mapped("/tv/a/b.mkv"), h.mapped("/a/b.mkv", back=True)) == ("/", "/a/b.mkv", "/tv/a/b.mkv")
    monkeypatch.setattr(h, "PATH_MAP", [("/app", "/local")])
    assert h.app_query("manualimport?q=&folder=%2Flocal%2Fa") == "manualimport?q=&folder=%2Fapp%2Fa"


def test_arr_maps_the_answer_the_query_and_the_body(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(h, "PATH_MAP", [("/data", "/mnt/data")])
    monkeypatch.setitem(h.CFG, "RADARR_API_KEY", "k3y")
    monkeypatch.setattr(h, "http", lambda url, method="GET", body=None, headers=None, timeout=15: seen.append((url, method, body, headers))
                        or {"path": "/data/movies/a.mkv", "folder": "/other/x"})
    assert h.arr("radarr", "parse?title=A&path=%2Fmnt%2Fdata%2Fmovies%2Fa.mkv") == {"path": "/mnt/data/movies/a.mkv", "folder": "/other/x"}
    assert seen[0][0].endswith("/api/v3/parse?title=A&path=%2Fdata%2Fmovies%2Fa.mkv") and seen[0][3] == {"X-Api-Key": "k3y"}
    h.arr_write("radarr", "command", "POST", {"name": "ManualImport", "files": [{"path": "/mnt/data/movies/a.mkv", "movieId": 7}]})
    assert seen[1][2] == {"name": "ManualImport", "files": [{"path": "/data/movies/a.mkv", "movieId": 7}]}


def test_the_api_key_comes_from_the_env_file_before_config_xml(monkeypatch, tmp_path):
    conf = tmp_path / "config.xml"
    conf.write_text("<Config><ApiKey>fromxml</ApiKey></Config>")
    monkeypatch.setitem(h.CFG, "SONARR_DIR", str(tmp_path))
    monkeypatch.setitem(h.CFG, "SONARR_API_KEY", "")
    assert h.api_key("sonarr") == "fromxml"
    monkeypatch.setitem(h.CFG, "SONARR_API_KEY", "fromenv")
    assert h.api_key("sonarr") == "fromenv"
    assert h.mask("key fromenv") == "key <SONARR_API_KEY>"


def test_config_refuses_to_start_without_credentials_or_with_a_bad_setting(monkeypatch):
    base = {"WEBHOOK_USER": "guard", "WEBHOOK_PASSWORD": "s3cret-pass", "PATH_MAP": "", "AUDIT_TIME": "07:30"}
    for k, v in base.items():
        monkeypatch.setitem(h.CFG, k, v)
    assert arr_serve.config(h) == (AUTH.encode(), "07:30")
    monkeypatch.setitem(h.CFG, "AUDIT_TIME", "")
    assert arr_serve.config(h)[1] == ""
    for k, v in (("WEBHOOK_PASSWORD", ""), ("WEBHOOK_USER", ""), ("WEBHOOK_USER", "a:b"), ("WEBHOOK_PASSWORD", "pässword"),
                 ("AUDIT_TIME", "7.30")):
        monkeypatch.setitem(h.CFG, k, v)
        with pytest.raises(SystemExit):
            arr_serve.config(h)
        monkeypatch.setitem(h.CFG, k, base[k])
    monkeypatch.delitem(h.CFG, "AUDIT_TIME")
    assert arr_serve.config(h)[1] == "07:30"   # the default, the time of the native timer
    monkeypatch.setattr(h, "PATH_MAP_ERROR", "PATH_MAP takes pairs APP_PATH:LOCAL_PATH of absolute paths")
    with pytest.raises(SystemExit, match="PATH_MAP takes pairs"):   # a map that left a pair out would edit the wrong paths
        arr_serve.config(h)


def test_plex_gets_the_apps_path(monkeypatch):
    monkeypatch.setattr(h, "PATH_MAP", [("/data", "/mnt/data")])
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
    monkeypatch.setattr(arr_serve.Handler, "h", h)
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
    assert json.load(open(os.path.join(h.queue_dir(), name)))["file_id"] == "31"
    assert not os.path.exists(h.CFG["LOG"])   # a queued job leaves no decision line of its own


@pytest.mark.parametrize("auth", [None, "Basic " + base64.b64encode(b"guard:wrong").decode(), "Bearer x", AUTH + "x"])
def test_a_post_without_the_right_credentials_is_refused_before_the_body(server, app, auth):
    headers = {"Authorization": auth} if auth else {"Authorization": ""}
    code, text, hdrs = server("POST", "/radarr", radarr_body(app), headers=headers)
    assert code == 401 and hdrs.get("WWW-Authenticate") == 'Basic realm="arr-media-guard"'
    assert h.queued() == [] and app["calls"] == [] and not os.path.exists(h.CFG["LOG"])   # counted, never logged
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
    assert code == 500 and "/nonexistent/anime" in text and "PATH_MAP" in text
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


def test_an_api_failure_answers_502(server, app):
    app["api"]["moviefile/31"] = urllib.error.URLError("refused")
    code, text, _ = server("POST", "/radarr", radarr_body(app))
    assert code == 502 and h.queued() == [] and log_lines()[0]["result"] == "error"


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
    monkeypatch.setattr(arr_serve.Handler, "h", h)
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


def test_an_api_key_never_reaches_the_answer_or_the_log(server, app, monkeypatch, capsys):
    monkeypatch.setitem(h.CFG, "RADARR_API_KEY", "s3cretkey1234")
    app["api"]["moviefile/31"] = urllib.error.URLError("refused, key s3cretkey1234")
    code, text, _ = server("POST", "/radarr", radarr_body(app))
    assert code == 502 and "<RADARR_API_KEY>" in text and "s3cretkey1234" not in text
    app["api"]["moviefile/31"] = arr_serve.Refused(400, "the key s3cretkey1234 was named")
    server("POST", "/radarr", radarr_body(app))
    lines, out = open(h.CFG["LOG"]).read(), capsys.readouterr().out
    assert "s3cretkey1234" not in lines + out and lines.count("<RADARR_API_KEY>") == 2 and out.count("<RADARR_API_KEY>") == 2


def test_a_log_that_does_not_take_the_line_still_answers(server, app, monkeypatch, capsys):
    monkeypatch.setattr(h, "log", lambda rec: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert server("POST", "/radarr", radarr_body(app, movie={"id": 8}))[0] == 400
    assert "the decision log did not take the line: [Errno 28] No space left on device" in capsys.readouterr().out


def test_the_healthcheck_needs_no_credentials(server):
    assert server("GET", "/health", headers={"Authorization": ""})[:2] == (200, "ok\n")
    assert server("GET", "/radarr")[0] == 404


# --- the worker and the daily jobs --------------------------------------------------------------------------------------

def test_kick_starts_a_worker_only_when_work_waits_and_no_worker_runs(app, monkeypatch):
    started = []
    monkeypatch.setattr(arr_serve, "spawn", lambda h, what: started.append((what, h.try_lock("worker.lock") is not None)) or 4242)
    assert arr_serve.kick(h) is None and started == []   # no work
    open(os.path.join(h.queue_dir(), "1-1.json"), "w").close()
    held = h.try_lock("worker.lock")
    assert arr_serve.kick(h) is None and started == []   # a worker runs
    held.close()
    assert arr_serve.kick(h) == 4242 and started == [("worker", True)]   # the new worker takes the lock itself


def test_spawn_starts_a_new_program_in_its_own_session(monkeypatch):
    seen = []
    monkeypatch.setattr(arr_serve.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    arr_serve.spawn(h, "worker")
    (argv, kw), = seen
    assert argv[1:] == [os.path.realpath(h.__file__), "--serve", "--worker"] and kw == {"start_new_session": True}


def test_the_worker_and_daily_modes_run_their_part(app, monkeypatch):
    ran = []
    monkeypatch.setattr(h, "worker", lambda lock: ran.append(("worker", h.try_lock("worker.lock") is None)))
    monkeypatch.setattr(arr_serve, "daily", lambda h: ran.append(("daily", None)))
    arr_serve.main(h, ["--worker"])
    held = h.try_lock("worker.lock")
    arr_serve.main(h, ["--worker"])   # another worker runs: this one exits
    held.close()
    arr_serve.main(h, ["--daily"])
    assert ran == [("worker", True), ("daily", None)]
    with pytest.raises(SystemExit, match="usage"):
        arr_serve.main(h, ["--other"])


@pytest.mark.parametrize("left", ["queue/1-1.json", "claimed/1-1.json", "plex-pending.json", "deep-analysis/deep-analysis-ab.json"])
def test_work_waits_for_a_queued_or_claimed_job_or_kept_plex_analyzes(app, left):
    assert not arr_serve.waiting(h)
    os.makedirs(os.path.join(h.CFG["STATE_DIR"], "deep-analysis"), exist_ok=True)
    open(os.path.join(h.CFG["STATE_DIR"], ".tmp-x"), "w").close()
    open(os.path.join(h.CFG["STATE_DIR"], "queue", ".1-1.json"), "w").close()   # half a job is no job
    assert not arr_serve.waiting(h)
    open(os.path.join(h.CFG["STATE_DIR"], left), "w").close()
    assert arr_serve.waiting(h)


def test_the_daily_jobs_run_once_a_day_after_their_time(app):
    day = datetime.datetime(2026, 9, 30, 7, 29)
    assert not arr_serve.daily_due(h, "07:30", day)
    assert arr_serve.daily_due(h, "07:30", day.replace(minute=30))
    assert not arr_serve.daily_due(h, "07:30", day.replace(hour=23))
    assert arr_serve.daily_due(h, "07:30", day + datetime.timedelta(days=1, hours=5))   # a missed day runs at the next look
    assert not arr_serve.daily_due(h, "", day + datetime.timedelta(days=3))


def test_the_daily_jobs_audit_each_app_with_a_key_then_rotate_the_log(app, monkeypatch):
    runs = []
    monkeypatch.setitem(h.CFG, "RADARR_API_KEY", "k")
    monkeypatch.setitem(h.CFG, "SONARR_API_KEY", "")
    monkeypatch.setitem(h.CFG, "SONARR_DIR", "/nonexistent")
    monkeypatch.setattr(arr_serve.subprocess, "run", lambda argv, **kw: runs.append(argv))
    arr_serve.daily(h)
    assert [r[2:] for r in runs[:-1]] == [["--audit", "radarr", "--since", "24h", "--post"]]
    state = os.path.join(h.CFG["STATE_DIR"], "logrotate.state")
    assert runs[-1][:3] == ["logrotate", "-s", state] and runs[-1][-1].endswith("logrotate.conf")   # the state stays in /config
    assert open(runs[-1][-1]).read().startswith(h.CFG["LOG"] + " {\n    weekly\n")


def test_the_daily_jobs_go_on_without_logrotate(app, monkeypatch, capsys):
    monkeypatch.setitem(h.CFG, "RADARR_API_KEY", "")
    monkeypatch.setitem(h.CFG, "RADARR_DIR", "/nonexistent")
    monkeypatch.setitem(h.CFG, "SONARR_DIR", "/nonexistent")
    def run(argv, **kw):
        if argv[0] == "logrotate":
            raise FileNotFoundError(2, "No such file or directory", "logrotate")
    monkeypatch.setattr(arr_serve.subprocess, "run", run)
    arr_serve.daily(h)
    assert "logrotate is not installed, so the decision log is not rotated" in capsys.readouterr().out


def test_an_app_whose_config_xml_holds_no_key_is_left_out(app, monkeypatch, tmp_path):
    (tmp_path / "config.xml").write_text("<Config><Port>8989</Port></Config>")
    monkeypatch.setitem(h.CFG, "SONARR_DIR", str(tmp_path))
    monkeypatch.setitem(h.CFG, "SONARR_API_KEY", "")
    monkeypatch.setitem(h.CFG, "RADARR_API_KEY", "k")
    assert arr_serve.apps_on(h) == ["radarr"]


FAKE_WORKER = """
import os, signal, subprocess, sys, time
queue, marks, kid = sys.argv[1:]
mark = lambda text: open(marks, "a").write(text + "\\n")
for n in os.listdir(queue):
    os.remove(os.path.join(queue, n))
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


def test_the_listener_queues_starts_a_worker_and_stops_on_sigterm(app, monkeypatch, tmp_path):
    """The real main loop in a forked child: a Download post, the worker it starts, and a stop by SIGTERM. The worker is
    a program in its own session, as spawn() starts it. It blocks SIGTERM for a while, as no_stop() does during a flag
    edit, then keeps the default action, as worker() does. The listener must stop it and wait for it."""
    port, marks, kid = free_port(), tmp_path / "marks", tmp_path / "kid"
    for k, v in (("WEBHOOK_USER", "guard"), ("WEBHOOK_PASSWORD", "s3cret-pass"), ("AUDIT_TIME", "")):
        monkeypatch.setitem(h.CFG, k, v)
    for d in ("claimed", "alerts"):   # the listener makes the state folders itself
        os.rmdir(os.path.join(h.CFG["STATE_DIR"], d))
    monkeypatch.setattr(arr_serve, "PORT", port)
    monkeypatch.setattr(arr_serve, "POLL", 0.1)   # the listener sees the stop at once, while the worker holds its edit
    monkeypatch.setattr(arr_serve, "spawn", lambda h, what: subprocess.Popen(
        [sys.executable, "-c", FAKE_WORKER, h.queue_dir(), str(marks), str(kid)], start_new_session=True))
    out = tmp_path / "stdout"
    pid = os.fork()
    if not pid:
        try:
            sys.stdout = open(out, "w", buffering=1)   # the listener's lines, for the parent to read
            arr_serve.main(h, [])
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
        assert all(os.path.isdir(os.path.join(h.CFG["STATE_DIR"], d)) for d in ("claimed", "alerts"))
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


def test_the_listener_runs_the_daily_jobs_again_once_the_last_run_ended(app, monkeypatch, tmp_path):
    """A daily run is a child of the listener. The listener reaps it, so the next day's run starts."""
    port, marks, due = free_port(), tmp_path / "daily", iter([True, True, True])
    for k, v in (("WEBHOOK_USER", "guard"), ("WEBHOOK_PASSWORD", "s3cret-pass"), ("AUDIT_TIME", "00:00")):
        monkeypatch.setitem(h.CFG, k, v)
    monkeypatch.setattr(arr_serve, "PORT", port)
    monkeypatch.setattr(arr_serve, "POLL", 0.05)
    monkeypatch.setattr(arr_serve, "TICK", 0.2)
    monkeypatch.setattr(arr_serve, "daily_due", lambda h, at, now=None: next(due, False))
    monkeypatch.setattr(arr_serve, "spawn", lambda h, what: subprocess.Popen([sys.executable, "-c", f"open({str(marks)!r}, 'a').write('run\\n')"]))
    pid = os.fork()
    if not pid:
        try:
            arr_serve.main(h, [])
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
    monkeypatch.setattr(arr_serve.Handler, "h", h)
    monkeypatch.setattr(arr_serve.Handler, "auth", AUTH.encode())
    srv = arr_serve.Server(("127.0.0.1", 0), arr_serve.Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
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
    assert arr_serve.REFUSALS.n == 1 and not os.path.exists(h.CFG["LOG"])


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
    monkeypatch.setattr(arr_serve.Handler, "h", h)
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
        t.join(20)


def test_refused_posts_are_counted_and_summed_up_once_a_minute(server, app, capsys):
    for _ in range(30):
        assert server("POST", "/radarr", radarr_body(app), headers={"Authorization": "Basic d3Jvbmc6d3Jvbmc="})[0] == 401
        assert server("POST", "/lidarr", {})[0] == 404
    assert not os.path.exists(h.CFG["LOG"]) and "POST /radarr" not in capsys.readouterr().out   # no line per request
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
import importlib.machinery, importlib.util, os, sys
sys.path.insert(0, sys.argv[1])
loader = importlib.machinery.SourceFileLoader("amg", os.path.join(sys.argv[1], "arr-media-guard"))
h = importlib.util.module_from_spec(importlib.util.spec_from_loader("amg", loader))
loader.exec_module(h)
import arr_serve
arr_serve.PORT, arr_serve.POLL = int(sys.argv[2]), 0.1
arr_serve.main(h, [])
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


def test_a_missing_recycle_bin_warns_in_the_test_and_the_selftest_and_fails_neither(server, app, monkeypatch, capsys):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}
    code, text, _ = server("POST", "/radarr", {"eventType": "Test"})
    assert code == 200 and "Warning: Radarr has no recycle bin, so the restore after a bad upgrade cannot work." in text
    assert "arr-media-guard: radarr warning: Radarr has no recycle bin" in capsys.readouterr().out
    monkeypatch.setitem(h.CFG, "RADARR_API_KEY", "k")
    monkeypatch.setitem(h.CFG, "SONARR_DIR", "/nonexistent")   # an app this host does not run: no warning, no failure
    h.main(["--selftest"])
    out = capsys.readouterr().out
    assert "warning: Radarr has no recycle bin" in out and "Sonarr" not in out and out.rstrip().endswith("selftest ok")


def test_a_config_xml_with_no_key_gives_no_warning_in_the_selftest(app, monkeypatch, capsys, tmp_path):
    app["api"]["config/mediamanagement"] = {"recycleBin": ""}   # a bin warning for any app the selftest asks
    (tmp_path / "config.xml").write_text("<Config><Port>8989</Port></Config>")
    monkeypatch.setitem(h.CFG, "SONARR_DIR", str(tmp_path))
    monkeypatch.setitem(h.CFG, "RADARR_DIR", "/nonexistent")
    for k in ("SONARR_API_KEY", "RADARR_API_KEY"):
        monkeypatch.delitem(h.CFG, k, raising=False)
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


def test_a_recycle_bin_beside_the_media_gives_no_warning(app):
    assert h.bin_warnings("sonarr") == [] and h.bin_warnings("radarr") == []
