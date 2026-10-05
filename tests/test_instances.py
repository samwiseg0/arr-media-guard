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
"""Named app instances: two Sonarr instances in one install, each with its own URL, API key, path map, re-grab cap,
decision lines and alerts, through the host hook and through the listener. Each test loads its own copy of the package
with an env file that sets them up, see amg.load(). The apps are a fake behind the real arr(), so each call shows its
URL and its key.

Run: pytest tests/test_instances.py
"""
import base64
import copy
import dataclasses
import http.client
import itertools
import json
import io
import os
import threading
import urllib.parse
import urllib.request
import urllib.response

import pytest

import amg

FILES = os.path.join(os.path.dirname(__file__), "..")
POLICY = os.path.abspath(os.path.join(FILES, "examples", "policy.json"))
COUNT = itertools.count()
AUTH = "Basic " + base64.b64encode(b"guard:s3cret").decode()
HOSTS = {"sonarr": "hd.invalid:8989", "sonarr-4k": "uhd.invalid:8990"}
KEYS = {"sonarr": "key-hd", "sonarr-4k": "key-uhd"}
NAME = "Show A - S01E01 - One WEBDL-1080p.mkv"


@pytest.fixture
def g(tmp_path, monkeypatch):
    """A copy of the package with two Sonarr instances, sonarr and sonarr-4k. Both list series 5 with episode file 9 at
    the app path /tv/Show A/Season 1/<NAME>, and each map puts /tv in its own local folder, hd or uhd. g.calls holds
    (host, API key, path) of each API call, and g.posts the username and footer of each Discord post."""
    for d in ("hd", "uhd"):
        os.makedirs(tmp_path / d / "Show A" / "Season 1")
        (tmp_path / d / "Show A" / "Season 1" / NAME).write_bytes(b"x")
    os.makedirs(tmp_path / "state")
    env = tmp_path / "env"
    env.write_text(f"POLICY_FILE='{POLICY}'\nSTATE_DIR='{tmp_path / 'state'}'\nLOG='{tmp_path / 'log.jsonl'}'\nINSTANCE='box'\n"
                   f"APP_INSTANCES='sonarr-4k:sonarr'\nRADARR_DIR='{tmp_path / 'no-radarr'}'\n"
                   f"SONARR_URL='http://{HOSTS['sonarr']}'\nSONARR_API_KEY='{KEYS['sonarr']}'\nSONARR_PATH_MAP='/tv:{tmp_path / 'hd'}'\n"
                   f"SONARR_4K_URL='http://{HOSTS['sonarr-4k']}'\nSONARR_4K_API_KEY='{KEYS['sonarr-4k']}'\n"
                   f"SONARR_4K_PATH_MAP='/tv:{tmp_path / 'uhd'}'\nREGRAB_CAP='1'\nKEEP_REPLACED='true'\n"
                   "DISCORD_WEBHOOK='https://discord.invalid/hook'\nWEBHOOK_USER='guard'\nWEBHOOK_PASSWORD='s3cret'\n")
    monkeypatch.setenv("ARR_MEDIA_GUARD_ENV", str(env))
    monkeypatch.setenv("ARR_MEDIA_GUARD_LIB", FILES)
    m = amg.load(f"arr_media_guard_instances{next(COUNT)}")
    assert m.CFG.errors == [] and list(m.CFG.apps) == ["radarr", "sonarr", "sonarr-4k"]
    calls, posts, syslog = [], [], []
    api = {host: {"series/5": {"id": 5, "title": f"Show A {app}", "path": "/tv/Show A", "originalLanguage": {"name": "English"}},
                  "episodefile/9": {"id": 9, "seriesId": 5, "path": f"/tv/Show A/Season 1/{NAME}", "sceneName": "Show.A.S01E01.1080p-GRP"},
                  "episode?episodeFileId=9": [{"id": 31, "seasonNumber": 1, "episodeNumber": 1, "runtime": 22, "seriesId": 5, "episodeFileId": 9}],
                  "episode?episodeIds=31": [{"id": 31, "seasonNumber": 1, "episodeNumber": 1, "seriesId": 5, "episodeFileId": 9}],
                  "rootfolder": [{"path": "/tv"}], "config/mediamanagement": {"recycleBin": ""},
                  "notification": [{"name": "guard", "implementation": "Webhook", "onGrab": True, "fields": [{"name": "url", "value": f"http://guard:8484/{app}"},
                                                                                                       {"name": "username", "value": "guard"}]}],
                  "system/status": {"instanceName": {"sonarr": "Sonarr", "sonarr-4k": "Sonarr 4K"}[app]}}
           for app, host in HOSTS.items()}

    def fake_http(url, method="GET", body=None, headers=None, timeout=15):
        u = urllib.parse.urlparse(url)
        if u.netloc == "discord.invalid":
            posts.append((body["username"], body["embeds"][0]["footer"]["text"]))
            return b""
        path = u.path.split("/api/v3/", 1)[1] + (f"?{u.query}" if u.query else "")
        calls.append((u.netloc, (headers or {}).get("X-Api-Key"), path))
        return copy.deepcopy(api[u.netloc][path])
    monkeypatch.setattr(m, "http", fake_http)
    guard = urllib.request.urlopen   # conftest's no_network

    def urlopen(req, *args, **kwargs):   # the start check's GET of the webhook, which answers with its details
        if req.full_url == "https://discord.invalid/hook" and req.get_method() == "GET":
            return urllib.response.addinfourl(io.BytesIO(b'{"id": "1"}'), {}, req.full_url, 200)
        return guard(req, *args, **kwargs)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(m, "to_syslog", syslog.append)
    monkeypatch.setattr(m, "mount_top", lambda f: str(tmp_path))   # the kept folders stay in tmp_path
    for k in list(os.environ):
        if k.startswith(("radarr_", "sonarr_")):
            monkeypatch.delenv(k)
    for k, v in dict(tmp=tmp_path, calls=calls, posts=posts, syslog=syslog, api=api).items():   # the test's own records, beside the package names
        object.__setattr__(m, k, v)
    return m


def local(g, app):
    """The local path of episode file 9 of app, by its own map."""
    return str(g.tmp / {"sonarr": "hd", "sonarr-4k": "uhd"}[app] / "Show A" / "Season 1" / NAME)


def lines(g):
    with open(g.CFG.log) as f:
        return [json.loads(line) for line in f]


def run_queued(g, monkeypatch):
    """Run each queued job through run_job(), with process() faked to post one alert. Returns the Ctx of each job."""
    seen = []

    def fake_process(ctx):
        seen.append(ctx)
        rec = {"app": ctx.app, "source": "hook", "label": ctx.label, "path": ctx.path, "outcome": "no_change", "result": "no change",
               "findings": [{"kind": "language", "want": "English", "has": ["fre"]}], "alert_kinds": ["language"]}
        rec["alert_result"] = g.alert_findings(rec, 1)
        return rec
    monkeypatch.setattr(g, "process", fake_process)
    for name in g.queued():
        g.run_job(name, [])
    return seen


def hook_run(g, monkeypatch, said, event="Download", **env):
    """hook() for a Sonarr Custom Script run whose Instance Name is said. No worker starts."""
    monkeypatch.setattr(g, "try_lock", lambda name: None)
    for k, v in dict(sonarr_eventtype=event, sonarr_instancename=said, **env).items():
        monkeypatch.setenv(k, v)
    g.hook()


# --- the host hook -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("said, app", [("Sonarr", "sonarr"), ("Sonarr-4K", "sonarr-4k"), ("Sonarr 4K", "sonarr-4k"), ("sonarr_4k", "sonarr-4k")])
def test_a_hook_run_goes_to_the_instance_its_instance_name_names(g, monkeypatch, capsys, said, app):
    """Sonarr sets sonarr_instancename at each event. Its job, its API calls, its decision line, its logfmt line and its
    alert are all of that instance, by its URL, its key and its path map."""
    hook_run(g, monkeypatch, said, sonarr_series_id="5", sonarr_episodefile_id="9", sonarr_episodefile_path=local(g, app),
             sonarr_download_id="SABnzbd_nzo_1")
    (name,) = g.queued()
    assert g.job_of(name)["app"] == app
    (ctx,) = run_queued(g, monkeypatch)
    assert (ctx.app, ctx.path, ctx.label) == (app, local(g, app), f"Show A {app} S01E01")
    assert {c[:2] for c in g.calls if c[2] != "system/status"} == {(HOSTS[app], KEYS[app])}   # the other instance gives no data
    assert sorted(c[0] for c in g.calls if c[2] == "system/status") == sorted(HOSTS.values())   # each name once, see job_refused()
    (line,) = lines(g)
    assert (line["app"], line["instance"], line["outcome"]) == (app, "box", "no_change")
    assert g.syslog[0].startswith(f"arr={app} source=hook outcome=no_change")
    assert g.posts == [({"sonarr": "Sonarr box", "sonarr-4k": "Sonarr-4k box"}[app], "arr-media-guard on box")]


def test_a_hook_run_whose_instance_name_names_no_instance_is_refused(g, monkeypatch, capsys):
    """With two Sonarr instances, a run whose Instance Name fits neither asks no app, queues nothing, fails a Test, and
    logs why."""
    for event in ("Download", "Test"):
        with pytest.raises(SystemExit) as ex:
            hook_run(g, monkeypatch, "Sonarr HD", event, sonarr_series_id="5", sonarr_episodefile_id="9",
                     sonarr_episodefile_path=local(g, "sonarr"))
        assert str(ex.value) == ("arr-media-guard: Sonarr names its instance 'Sonarr HD', and no sonarr instance in APP_INSTANCES has that name. "
                                 "Set the Instance Name in Settings, General to match one of sonarr, sonarr-4k, as 'Sonarr', 'Sonarr-4k'. Case "
                                 "does not count, and a space counts as '-'.")
    assert g.queued() == [] and g.calls == []
    assert [(r["result"], r["note"].split(":")[0]) for r in lines(g)] == [("error", "the Download event was not taken"),
                                                                         ("error", "the Test event was not taken")]


def test_the_one_instance_of_a_program_takes_every_run_whatever_its_name(g, monkeypatch):
    """A host with one Radarr needs no setting, whatever Instance Name Radarr has."""
    monkeypatch.setenv("radarr_instancename", "Radarr Movies")
    assert g.hook_app("radarr") == ("radarr", None)
    monkeypatch.delenv("radarr_instancename")
    assert g.hook_app("radarr") == ("radarr", None)


def test_the_hook_test_checks_the_instance_it_names(g, monkeypatch, capsys):
    """The Test checks the API of the instance it names, and the Instance Name of every Sonarr instance."""
    hook_run(g, monkeypatch, "Sonarr-4K", "Test")
    assert capsys.readouterr().out.splitlines()[-1] == "arr-media-guard: Test ok"
    assert {c[:2] for c in g.calls if c[2] != "system/status"} == {(HOSTS["sonarr-4k"], KEYS["sonarr-4k"])}
    assert sorted(c[0] for c in g.calls if c[2] == "system/status") == sorted(HOSTS.values())


CLASH = ("Sonarr-4k names its instance {!r}, so its Custom Script runs go to {}. Set its Instance Name in Settings, General to "
         "match sonarr-4k, for example 'Sonarr 4k'.")


@pytest.mark.parametrize("said, goes", [("Sonarr", "the instance sonarr"), ("Sonarr UHD", "no instance")])
def test_a_misnamed_instance_fails_the_host_test_of_every_instance_and_names_the_name_to_set(g, monkeypatch, said, goes):
    """A 4K Sonarr that keeps the Instance Name "Sonarr" sends its Test to the instance sonarr, whose own name fits. The
    Test still fails, because a run of the 4K Sonarr would ask the default API with the ids and paths of the 4K one."""
    g.api[HOSTS["sonarr-4k"]]["system/status"]["instanceName"] = said
    for name in ("Sonarr", "Sonarr 4K"):
        with pytest.raises(SystemExit) as ex:
            hook_run(g, monkeypatch, name, "Test")
        assert str(ex.value) == "arr-media-guard: " + CLASH.format(said, goes)
    assert g.queued() == []


def queue_two_imports(g, monkeypatch, said, app):
    """Two hook runs of episode file 9 of app, each with the Instance Name said."""
    for n in (1, 2):
        hook_run(g, monkeypatch, said, sonarr_series_id="5", sonarr_episodefile_id="9", sonarr_episodefile_path=local(g, app),
                 sonarr_download_id=f"SABnzbd_nzo_{n}")
    assert len(g.queued()) == 2 and {g.job_of(n)["instance_name"] for n in g.queued()} == {said}


def test_the_worker_reads_each_instance_name_once_and_runs_a_job_whose_name_fits(g, monkeypatch):
    queue_two_imports(g, monkeypatch, "Sonarr 4K", "sonarr-4k")
    assert [c.app for c in run_queued(g, monkeypatch)] == ["sonarr-4k", "sonarr-4k"]
    assert sorted(c[0] for c in g.calls if c[2] == "system/status") == sorted(HOSTS.values())


def test_the_worker_refuses_a_job_while_two_apps_share_its_instance_name(g, monkeypatch):
    """A 4K Sonarr whose connection was saved before APP_INSTANCES named it, and that kept the name "Sonarr", sends its
    imports to the instance sonarr. Its Test never runs again. The worker finds the name clash, asks no app for the
    file, and logs one error line per job that names the Instance Name to set."""
    g.api[HOSTS["sonarr-4k"]]["system/status"]["instanceName"] = "Sonarr"
    queue_two_imports(g, monkeypatch, "Sonarr", "sonarr-4k")
    assert {g.job_of(n)["app"] for n in g.queued()} == {"sonarr"}
    assert run_queued(g, monkeypatch) == [] and g.queued() == []
    assert {c[2] for c in g.calls} == {"system/status"} and len(g.calls) == 2 and g.posts == []
    assert [(r["app"], r["outcome"], r["result"]) for r in lines(g)] == [("sonarr", "error", "error: the run named 'Sonarr' may come from another "
                                                                          "instance, so the job was not checked. " + CLASH.format("Sonarr", "the instance sonarr"))] * 2


def test_a_job_of_the_listener_or_of_a_single_instance_skips_the_name_check(g, monkeypatch):
    g.api[HOSTS["sonarr-4k"]]["system/status"]["instanceName"] = "Sonarr"
    real = g.arr
    monkeypatch.setattr(g, "arr", lambda app, p: pytest.fail(f"asked {app} for {p}"))
    assert g.job_refused({"app": "sonarr", "path": "/x"}) is None   # a Webhook job: its URL path picked the instance
    assert g.job_refused({"app": "radarr", "instance_name": "Radarr Movies"}) is None   # one Radarr takes every run
    monkeypatch.setattr(g, "arr", real)
    assert g.job_refused({"app": "sonarr", "instance_name": "Sonarr"}).startswith("the run named 'Sonarr' may come from another instance")


def test_a_misnamed_instance_warns_in_the_selftest_and_never_in_the_listener(g, post, monkeypatch, capsys):
    """The URL path of the Webhook picks the instance, so the listener's Test and start check skip the Instance Name. The
    owner saw this line at the first start of 2.1.0 in Docker:
    arr-media-guard: radarr warning: Radarr names its instance 'RadarrHD', so its Custom Script runs go to no instance. ...
    The selftest on a host names the clash and passes."""
    g.api[HOSTS["sonarr-4k"]]["system/status"]["instanceName"] = "Sonarr"
    clash = CLASH.format("Sonarr", "the instance sonarr")
    monkeypatch.setattr(g, "SERVE", True)   # serve.main() sets it in the listener
    assert post("/sonarr-4k", {"eventType": "Test"}) == (200, "arr-media-guard: Test ok.\n")
    assert post("/sonarr", {"eventType": "Test"}) == (200, "arr-media-guard: Test ok.\n")
    capsys.readouterr()
    g.arr_serve.start_check()
    out = capsys.readouterr().out.splitlines()
    assert "warning" not in " ".join(out) and "arr-media-guard: sonarr-4k start check: ok" in out
    assert not [c for c in g.calls if c[2] == "system/status"]
    monkeypatch.setattr(g, "SERVE", False)
    g.main(["--selftest"])
    out = capsys.readouterr().out.splitlines()
    assert f"warning: {clash}" in out and out[-1] == "selftest ok"
    monkeypatch.setattr(g, "IMAGE", True)   # docker exec ... --selftest: no app runs a Custom Script in the image
    g.main(["--selftest"])
    out = capsys.readouterr().out.splitlines()
    assert not [x for x in out if "warning" in x] and out[-1] == "selftest ok"
    with open(os.path.join(amg.ROOT, "docker", "Dockerfile")) as f:
        assert "ARR_MEDIA_GUARD_IMAGE=1" in f.read()


# --- the listener ------------------------------------------------------------------------------------------------------

@pytest.fixture
def post(g, monkeypatch):
    """The real handler on a free local port. post(path, body) makes one request and serves it."""
    serve = g.arr_serve
    monkeypatch.setattr(serve.Handler, "auth", AUTH.encode())
    monkeypatch.setattr(serve, "REFUSALS", serve.Refusals())
    srv = serve.Server(("127.0.0.1", 0), serve.Handler)

    def send(path, body):
        t = threading.Thread(target=srv.handle_request)
        t.start()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        c.request("POST", path, body=json.dumps(body).encode(), headers={"Authorization": AUTH})
        r = c.getresponse()
        out = (r.status, r.read().decode())
        c.close()
        t.join(20)
        return out
    yield send
    srv.server_close()


def sonarr_body(**kw):
    return dict({"eventType": "Download", "series": {"id": 5}, "episodes": [{"id": 31}], "downloadId": "SABnzbd_nzo_1",
                 "episodeFile": {"id": 9, "path": f"/tv/Show A/Season 1/{NAME}"}}, **kw)


@pytest.mark.parametrize("app", ["sonarr", "sonarr-4k"])
def test_each_instance_posts_to_its_own_path(g, post, monkeypatch, app):
    """The listener takes a post to /<name> for that instance: its API, its key and its path map. The worker's
    decision line and alert are of that instance too."""
    code, text = post(f"/{app}", sonarr_body())
    assert (code, text.strip()) == (200, f"arr-media-guard: queued {local(g, app)}")
    assert {c[:2] for c in g.calls} == {(HOSTS[app], KEYS[app])}
    (ctx,) = run_queued(g, monkeypatch)
    assert (ctx.app, ctx.path, ctx.label) == (app, local(g, app), f"Show A {app} S01E01")
    assert [r["app"] for r in lines(g)] == [app] and g.posts[0][0] == {"sonarr": "Sonarr box", "sonarr-4k": "Sonarr-4k box"}[app]


@pytest.mark.parametrize("path", ["/sonarr-8k", "/Sonarr-4K", "/sonarr-4k/", "/sonarr-4k?x=1", "/sonarr-4k/../sonarr", "/"])
def test_a_path_that_is_no_instance_gets_404_and_no_line(g, post, path):
    """Only an instance name passes the listener, so no other text reaches the API, a path or the decision log."""
    code, text = post(path, sonarr_body())
    assert code == 404 and "nothing listens here. Each instance posts to /<its name>" in text
    assert g.calls == [] and g.queued() == [] and not os.path.exists(g.CFG.log)


def test_the_test_event_and_the_start_check_run_per_instance(g, post, capsys):
    code, text = post("/sonarr-4k", {"eventType": "Test"})
    assert (code, text.strip()) == (200, "arr-media-guard: Test ok.")   # KEEP_REPLACED stands in for the recycle bin
    assert {c[:2] for c in g.calls} == {(HOSTS["sonarr-4k"], KEYS["sonarr-4k"])}
    g.calls.clear()
    capsys.readouterr()
    g.arr_serve.start_check()   # radarr has no API key, so it is not checked
    assert [x for x in capsys.readouterr().out.splitlines() if "start check" in x] == [
        "arr-media-guard: sonarr start check: ok", "arr-media-guard: sonarr-4k start check: ok", "arr-media-guard: discord start check: ok",
        "arr-media-guard: tmdb start check: ok"]
    assert {c[:2] for c in g.calls} == {(HOSTS[a], KEYS[a]) for a in HOSTS}
    assert g.arr_serve.apps_on() == ["sonarr", "sonarr-4k"]


def test_the_selftest_checks_each_instance(g, capsys):
    g.main(["--selftest"])
    assert capsys.readouterr().out.splitlines()[-1] == "selftest ok"
    assert {(c[0], c[2]) for c in g.calls if c[2] == "rootfolder"} == {(HOSTS[a], "rootfolder") for a in HOSTS}


# --- state per instance ------------------------------------------------------------------------------------------------

def test_the_regrab_cap_counts_per_instance(g):
    """REGRAB_CAP is 1. A re-grab of sonarr reaches its cap, and sonarr-4k still has its own."""
    assert g.count_regrab("sonarr") and not g.count_regrab("sonarr")
    assert g.count_regrab("sonarr-4k") and not g.count_regrab("sonarr-4k")
    assert {app: len(g.regrab_times(app)) for app in ("sonarr", "sonarr-4k", "radarr")} == {"sonarr": 1, "sonarr-4k": 1, "radarr": 0}


def test_grab_links_and_old_files_never_mix_between_instances_that_share_a_library(g, monkeypatch):
    """Both instances use the same download id. The grab link of sonarr goes stale when an import of sonarr-4k replaces
    the file, because the link no longer holds the file that sonarr's import replaced. An import of sonarr claims only
    its own link. The queued jobs of one instance never give another instance the old files of an upgrade."""
    p = local(g, "sonarr")
    assert g.keep_grab("sonarr", "5", "SABnzbd_nzo_1", [31])["kept"]
    g.claim_kept({"app": "sonarr-4k", "deleted": p, "download_id": "SABnzbd_nzo_1"})
    assert g.kept_copy("sonarr-4k", p, "SABnzbd_nzo_1") == (None, None)
    assert g.kept_copy("sonarr", p, "SABnzbd_nzo_1") == (None, "The kept copy is older, because another import replaced the file after the grab")
    later = g.time.time() + 5   # the next grab links into a stamp folder of its own
    monkeypatch.setattr(g.time, "time", lambda: later)
    assert g.keep_grab("sonarr", "5", "SABnzbd_nzo_2", [31])["kept"]
    g.claim_kept({"app": "sonarr", "deleted": p, "download_id": "SABnzbd_nzo_2"})
    rec, why = g.kept_copy("sonarr", p, "SABnzbd_nzo_2")
    assert why is None and rec["app"] == "sonarr" and rec["import"] == "SABnzbd_nzo_2"
    g.queue_job({"app": "sonarr-4k", "file_id": "9", "download_id": "SABnzbd_nzo_1", "deleted": "/tv/old.mkv", "recycled": ""})
    assert g.upgrade_jobs("sonarr", "SABnzbd_nzo_1") == {} and list(g.upgrade_jobs("sonarr-4k", "SABnzbd_nzo_1")) == [9]


def test_each_instance_reads_its_own_records_profiles_and_parse(g, monkeypatch):
    """movie_file(), kids_profiles() and the parse of an old file ask the instance they serve, and the TMDB lookup of
    any Radarr instance asks for a movie."""
    reads = []
    answers = {"moviefile/12": {"id": 12}, "qualityprofile": [{"id": 8, "name": "Kids"}]}
    monkeypatch.setattr(g, "arr", lambda app, p: reads.append((app, p.split("?")[0])) or answers.get(p, {"episodes": [{"id": 31}]}))
    assert g.movie_file({"movieFileId": 12, "movieFile": {"id": 11}}, "radarr-4k") == {"id": 12}
    g.kids_profiles("radarr-4k")
    g.kids_profiles("radarr")
    assert g.other_episodes("sonarr-4k", "/tv/Show A/Season 1/old.mkv", [31]) is None
    assert reads == [("radarr-4k", "moviefile/12"), ("radarr-4k", "qualityprofile"), ("radarr", "qualityprofile"), ("sonarr-4k", "parse")]
    asked = []
    monkeypatch.setattr(g.arr_meta, "expected_languages", lambda app, ids, **k: asked.append(app))
    monkeypatch.setattr(g, "CFG", dataclasses.replace(g.CFG, apps=dict(g.CFG.apps, **{"radarr-4k": g.CFG.apps["radarr"]})))
    g.tmdb_ask("radarr-4k", {"ids": {"tmdb": 1}})
    g.tmdb_ask("sonarr-4k", {"ids": {"tmdb": 1}})
    assert asked == ["radarr", "sonarr"]
