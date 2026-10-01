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
"""Unit tests for arr_subhunt.py, the subtitle hunter.

No network, no SABnzbd, no mkvtoolnix. The hook script is loaded by path, as in test_arr_media_guard.py, and
the hunter gets it as its host module. NZBHydra2, SABnzbd, the Radarr API, mkvmerge, the audio check and
Discord are fakes. The file lock is a real flock in a temp directory.

Run: pytest tests/test_arr_subhunt.py
"""
import copy
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import signal
import sqlite3
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
os.environ["ARR_MEDIA_GUARD_LIB"] = FILES
os.environ["ARR_MEDIA_GUARD_ENV"] = "/nonexistent/arr-media-guard.env"
_loader = importlib.machinery.SourceFileLoader("arr_media_guard_subhunt", os.path.join(FILES, "arr-media-guard"))
hook = importlib.util.module_from_spec(importlib.util.spec_from_loader("arr_media_guard_subhunt", _loader))
_loader.exec_module(hook)
import arr_subhunt as sh  # noqa: E402  (the hook put FILES on sys.path)
REAL_CREDS = sh.creds   # the fixture replaces creds, one test reads a real database

with open(os.path.join(FILES, "examples", "policy.json")) as _f:
    hook.arr_decide.set_policy(json.load(_f))

CURRENT = "Film.A.1972.1080p.HULU.WEB-DL.DDP5.1.H.264-GRP2"
MOVIE = {"id": 5017, "title": "Film A", "year": 1972, "runtime": 94, "imdbId": "tt0101", "tmdbId": 9001, "qualityProfileId": 11,
         "originalLanguage": {"id": 8, "name": "Japanese"}, "genres": ["Drama"],
         "movieFile": {"id": 7302, "sceneName": CURRENT, "size": 5800000000,
                       "quality": {"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080}}}}
PROFILE = {"id": 11, "name": "Profile A", "minFormatScore": 0,
           "items": [{"quality": {"id": 3, "name": "WEBDL-1080p"}, "allowed": True}, {"quality": {"id": 7, "name": "Bluray-1080p"}, "allowed": True},
                     {"quality": {"id": 6, "name": "Bluray-720p"}, "allowed": True}, {"quality": {"id": 30, "name": "Remux-1080p"}, "allowed": False},
                     {"quality": {"id": 18, "name": "WEBDL-2160p"}, "allowed": False},
                     {"name": "WEB 1080p", "allowed": True, "items": [{"quality": {"id": 15, "name": "WEBRip-1080p"}}]}],
           "formatItems": [{"format": 5, "name": "HEVC", "score": -20}, {"format": 9, "name": "AV1", "score": -100}]}
SIZES = [{"quality": {"id": 3}, "minSize": 15, "maxSize": 140}, {"quality": {"id": 7}, "minSize": 0, "maxSize": None}]
QUALITIES = {(1080, "bluray"): (7, "Bluray-1080p"), (1080, "webdl"): (3, "WEBDL-1080p"), (720, "bluray"): (6, "Bluray-720p")}
CREDS = {"sab": "https://sab.invalid/api", "sab_key": "SABKEY", "hydra": "https://search.invalid/api", "hydra_key": "HYDRAKEY"}


def probe(audio, subs, minutes=94):
    """mkvmerge -J of a file: one video track, the audio languages, and subtitles as (language, name, forced flag)."""
    tracks = [{"type": "video", "properties": {"uid": 1, "pixel_dimensions": "1920x1080"}}]
    tracks += [{"type": "audio", "properties": {"uid": 10 + i, "language": lang, "default_track": i == 0, "audio_channels": 2}}
               for i, lang in enumerate(audio)]
    tracks += [{"type": "subtitles", "properties": {"uid": 20 + i, "language": lang, "track_name": name, "default_track": False, "forced_track": forced}}
               for i, (lang, name, forced) in enumerate(subs)]
    return {"container": {"properties": {"duration": minutes * 60 * 10**9}}, "tracks": tracks}


NO_ENGLISH = probe(["jpn"], [("por", "", False)])
ENGLISH = probe(["jpn"], [("eng", "", False), ("por", "", False)])
SIGNS_ONLY = probe(["jpn"], [("eng", "Signs", False)])


def parse(title):
    """Radarr's /parse for a fake release name: quality from the name, the movie when the name is Film A, HEVC and AV1 formats."""
    t = title.lower()
    res = 2160 if "2160p" in t else 720 if "720p" in t else 1080
    qid, name = (30, "Remux-1080p") if "remux" in t else (18, "WEBDL-2160p") if res == 2160 else QUALITIES[(res, "bluray" if "bluray" in t else "webdl")]
    cfs = [{"id": 5, "name": "HEVC"}] * ("x265" in t) + [{"id": 9, "name": "AV1"}] * ("av1" in t)
    return {"parsedMovieInfo": {"quality": {"quality": {"id": qid, "name": name, "resolution": res}, "revision": {"version": 1}}},
            "movie": {"id": 5017} if t.startswith("film.a.") else None, "customFormats": cfs, "customFormatScore": -20 * len(cfs)}


def item(i, r):
    """One NZBHydra2 JSON item, as Hydra answers a movie query."""
    attrs = [("size", str(int(r.get("size", 6e9)))), ("hydraIndexerName", r.get("indexer", "IndexerA"))]
    attrs += [("imdb", "0101")] * r["title"].startswith("Film.A.")
    return {"title": r["title"], "link": f"https://search.invalid/getnzb/api/{i}?apikey=HYDRAKEY", "guid": str(i),
            "pubDate": 1780000000 - r.get("age", 0) * 86400, "attr": [{"attributes": {"name": k, "value": v}} for k, v in attrs]}


class FakeSab:
    """SABnzbd. A job is Grabbing (a dead link), Paused, Downloading (a stalled job) or Queued. A queued job finishes at
    the next history read. A plan is the storage folder, "fail:<message>", "grab", "stall" or "refuse". labels maps a
    release to SABnzbd's pause labels: such a job starts paused, as SABnzbd pauses an encrypted or duplicate job."""

    def __init__(self, plans, labels=None):
        self.plans, self.labels, self.jobs, self.calls = plans, labels or {}, {}, []

    def __call__(self, h, cred, **q):
        self.calls.append(q)
        mode, name, nzo = q["mode"], q.get("name"), q.get("value")
        if mode == "addurl":
            assert "cat" not in q and q["name"].startswith("https://search.invalid/getnzb/")   # no category, so Radarr never sees it
            nzo = f"nzo{len(self.calls)}"
            plan, labels = self.plans[q["nzbname"]], self.labels.get(q["nzbname"], [])
            if plan == "refuse":
                return {"status": False, "error": "no NZB in the answer"}
            status = {"grab": "Grabbing", "stall": "Downloading"}.get(plan) or ("Paused" if labels or q["priority"] == -2 else "Queued")
            self.jobs[nzo] = {"plan": plan, "status": status, "labels": labels}
            return {"status": True, "nzo_ids": [nzo]}
        if name == "resume":
            self.jobs[nzo]["status"] = "Queued"
            return {"status": True}
        if name == "delete":
            self.jobs.pop(nzo, None)
            return {"status": True}
        if mode == "queue":
            return {"queue": {"slots": [{"nzo_id": n, "status": j["status"], "labels": j["labels"]} for n, j in self.jobs.items()
                                        if j["status"] != "Queued"]}}
        return {"history": {"slots": [{"nzo_id": n, "status": "Failed", "fail_message": j["plan"][5:]} if j["plan"].startswith("fail:")
                                      else {"nzo_id": n, "status": "Completed", "storage": j["plan"]}
                                      for n, j in self.jobs.items() if j["status"] == "Queued"]}}

    def added(self):
        return [(q["nzbname"], q["priority"]) for q in self.calls if q["mode"] == "addurl"]

    def deleted(self):
        return {q["value"] for q in self.calls if q.get("name") == "delete"}

    def resumed(self):
        return [q["value"] for q in self.calls if q.get("name") == "resume"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake host: the Film A library file, the Radarr API, Hydra, SABnzbd and Discord. Returns the recorded calls.
    Set env["releases"] to [{"title", "probe" or "plan", "size", "age"}] before a run. A probe means the download completes."""
    lib = tmp_path / "movies" / "Film A"
    lib.mkdir(parents=True)
    (lib / "Film A WEBDL-1080p.mkv").write_bytes(b"old")
    (tmp_path / "state").mkdir()
    movie = copy.deepcopy(MOVIE)
    movie["movieFile"]["path"] = str(lib / "Film A WEBDL-1080p.mkv")
    old = movie["movieFile"]["path"]
    calls = {"movie": movie, "releases": [], "hydra": [], "imports": [], "posts": [], "land": True, "broken": {}, "signals": [],
             "probes": {old: NO_ENGLISH}, "syslog": [], "tmp": tmp_path, "old": old,
             "keep": str(lib / ".Film A WEBDL-1080p.mkv.subhunt-keep")}
    monkeypatch.setitem(hook.CFG, "LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setitem(hook.CFG, "STATE_DIR", str(tmp_path / "state"))

    def fake_http(url, method="GET", body=None, headers=None, timeout=15):
        assert url.startswith("https://search.invalid/api?"), url
        calls["hydra"].append(parse_qs(urlparse(url).query))
        return {"channel": {"item": [item(i, r) for i, r in enumerate(calls["releases"])]}}

    def fake_arr(app, p):
        q = parse_qs(urlparse(p).query)
        if p == "movie/5017": return copy.deepcopy(calls["movie"])
        if p == "qualityprofile": return [PROFILE]
        if p == "qualitydefinition": return SIZES
        if p == "rootfolder": return [{"path": str(tmp_path / "movies")}]
        if p.startswith("parse?"): return parse(q["title"][0])
        if p.startswith("command/"): return {"id": 1, "status": "completed"}
        assert p.startswith("manualimport?") and q["filterExistingFiles"] == ["false"], p
        return [{"path": os.path.join(q["folder"][0], n), "quality": {"quality": {"id": 7}}, "releaseGroup": "G"} for n in os.listdir(q["folder"][0])]

    def fake_write(app, p, method, body=None):
        assert (p, method) == ("command", "POST")
        with open(os.path.join(hook.CFG["STATE_DIR"], "lock")) as f:   # the import runs under the hook's file lock
            with pytest.raises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert os.path.samefile(calls["keep"], calls["old"])   # the keep link holds the old file before Radarr deletes it
        calls["imports"].append(body)
        os.remove(calls["old"])   # Radarr deletes the old file first, into its recycle bin
        if calls["land"]:
            new = body["files"][0]["path"]
            calls["movie"]["movieFile"] = dict(calls["movie"]["movieFile"], id=10001, size=os.path.getsize(new))
        return {"id": 1, "status": "queued"}

    def fake_post(app, emb):
        calls["posts"].append(emb)
        return "sent"

    monkeypatch.setattr(hook, "http", fake_http)
    monkeypatch.setattr(hook, "arr", fake_arr)
    monkeypatch.setattr(hook, "arr_write", fake_write)
    monkeypatch.setattr(hook, "mkvmerge", lambda p: copy.deepcopy(calls["probes"][p]))
    monkeypatch.setattr(hook, "check_audio", lambda p, j, edits, runtime=0: (calls["broken"].get(p), [], []))
    monkeypatch.setattr(hook, "post", fake_post)
    monkeypatch.setattr(hook, "to_syslog", calls["syslog"].append)
    monkeypatch.setattr(hook, "PROFILES", {})
    monkeypatch.setattr(hook, "OUTCOMES", hook.OUTCOMES)
    monkeypatch.setattr(sh, "creds", lambda h, app: CREDS)
    monkeypatch.setattr(sh, "GRAB_WAIT", 0)
    monkeypatch.setattr(sh, "DOWNLOAD_WAIT", 5)   # a loop that never ends fails the test in seconds
    monkeypatch.setattr(sh.time, "sleep", lambda s: None)
    monkeypatch.setattr(sh.signal, "signal", lambda sig, fn: calls["signals"].append((sig, fn)))
    return calls


def run(env, monkeypatch, *args, wrap=None):
    """Build the download folders, wire SABnzbd, run the hunter for Film A. Returns the fake SABnzbd.
    wrap(fake) returns the sab() the hunter gets, for a test that breaks SABnzbd."""
    plans = {}
    for r in env["releases"]:
        plans[r["title"]] = r.get("plan")
        if "probe" in r:
            folder = env["tmp"] / "downloads" / r["title"]
            folder.mkdir(parents=True, exist_ok=True)
            video = folder / (r["title"] + ".mkv")
            video.write_bytes(b"new " + r["title"].encode())
            env["probes"][str(video)] = r["probe"]
            plans[r["title"]] = str(folder)
    fake = FakeSab(plans, {r["title"]: r["labels"] for r in env["releases"] if "labels" in r})
    monkeypatch.setattr(sh, "sab", wrap(fake) if wrap else fake)
    sh.main(hook, ["radarr", "--ids", "5017", *args])
    return fake


def lines(env):
    with open(hook.CFG["LOG"]) as f:
        return [json.loads(line) for line in f]


def state(env):
    with open(os.path.join(hook.CFG["STATE_DIR"], "subhunt-radarr.json")) as f:
        return json.load(f)["5017"]


SUBS, NF, CRIT, NORD = ("Film.A.1972.1080p.BluRay.x264.Eng.Subs-GRP", "Film.A.1972.1080p.NF.WEB-DL.DDP5.1.H.264-XYZ",
                        "Film.A.1972.CRiTERiON.1080p.BluRay.x264-ABC", "Film.A.1972.1080p.NORDiC.WEB-DL.H.264-GRP7")


# --- ranking -------------------------------------------------------------------------------------

def test_rank_uses_the_apps_rules_and_puts_likely_english_subtitles_first(env):
    names = [SUBS, NF, CRIT, "Film.A.1972.1080p.BluRay.x264-PLAIN", "Film.A.1972.1080p.BluRay.x265-HEVCGRP", NORD, CURRENT,
             "Film.A.1972.2160p.WEB-DL.DDP5.1.H.265-GRP4", "Film.A.1972.1080p.BluRay.REMUX.AVC-GRP5", "Film.A.1972.720p.BluRay.x264-GRP6",
             "Film.A.1972.1080p.WEB-DL.HC.x264-HARD", "Film.A.1972.1080p.WEB-DL.AV1-VETO", "Film.A.1972.1080p.iT.WEB-DL-TRIED",
             "Film.B.1975.1080p.BluRay.x264-OTHER"]
    results = [dict(title=n, size=6e9, link=f"x{i}", pubDate=f"2026-01-{i + 10:02d}", indexer="G", attrs={}) for i, n in enumerate(names)]
    results.append(dict(title="Film.A.1972.1080p.WEB-DL.x264-TINY", size=0.5e9, link="t", pubDate="2026-01-01", indexer="G", attrs={}))
    results.append(dict(results[1], pubDate="2020-01-01", indexer="old"))   # an older post of the NF release
    results.append(dict(title="Film.A.1972.1080p.WEB-DL.H.264-GRP3 (NL subs)", size=6e9, link="n", pubDate="2026-01-02", indexer="G", attrs={}))
    results.append(dict(title="Film.A.1972.KOREAN.1080p.WEB-DL.H.264-KR", size=6e9, link="k", pubDate="2026-01-03", indexer="G",
                        attrs={"imdb": "90000001"}))   # Radarr maps the name, the indexer's IMDb tag says another film
    results.append(dict(results[3], pubDate="2026-02-01", attrs={"imdb": "90000001"}))   # one indexer mistags PLAIN, another tags it right
    results[3]["attrs"] = {"imdb": "tt0101"}
    ctx = {"profiles": {11: PROFILE}, "sizes": {s["quality"]["id"]: s for s in SIZES}}
    cands, skipped = sh.rank(hook, "radarr", MOVIE, results, [{"title": "Film.A.1972.1080p.iT.WEB-DL-TRIED"}], ctx)
    assert [c["title"] for c in cands] == [SUBS, NF, CRIT, "Film.A.1972.1080p.BluRay.x264-PLAIN",   # a tie goes to the newer post
                                           "Film.A.1972.1080p.WEB-DL.H.264-GRP3 (NL subs)", "Film.A.1972.1080p.BluRay.x265-HEVCGRP", NORD]
    assert cands[1]["indexer"] == "G" and "link" not in sh.brief(cands[0])   # the newest post, and never the link in a log
    assert cands[0]["signals"] == ["English subtitles tag"] and cands[4]["signals"] == [] and cands[6]["signals"] == ["local release"]
    assert skipped["another movie"] == ["Film.B.1975.1080p.BluRay.x264-OTHER", "Film.A.1972.KOREAN.1080p.WEB-DL.H.264-KR"]
    assert {k: v[0] for k, v in skipped.items()} == {
        "the current file": CURRENT, "WEBDL-2160p is not in the profile": "Film.A.1972.2160p.WEB-DL.DDP5.1.H.265-GRP4",
        "Remux-1080p is not in the profile": "Film.A.1972.1080p.BluRay.REMUX.AVC-GRP5", "below the current 1080p": "Film.A.1972.720p.BluRay.x264-GRP6",
        "hardcoded subtitles": "Film.A.1972.1080p.WEB-DL.HC.x264-HARD", "format AV1": "Film.A.1972.1080p.WEB-DL.AV1-VETO",
        "tried before": "Film.A.1972.1080p.iT.WEB-DL-TRIED", "another movie": "Film.B.1975.1080p.BluRay.x264-OTHER",
        "the size does not fit the runtime": "Film.A.1972.1080p.WEB-DL.x264-TINY"}


def test_verdict_names_every_reason_a_download_stays_out(env):
    j = probe(["eng"], [("eng", "Signs", False)], minutes=60)
    ts = hook.arr_decide.classify(j)
    assert sh.verdict(hook, "/d/x.mkv", j, ts, "Japanese", 94, False, "") == [
        "no full English subtitle (subtitles: eng forced)", "it runs 60 minutes, and the listed runtime is 94", "no Japanese audio (audio: eng)"]
    env["broken"]["/d/x.mkv"] = "silent audio"
    assert sh.verdict(hook, "/d/x.mkv", ENGLISH, hook.arr_decide.classify(ENGLISH), "Japanese", 94, False, "") == ["broken audio: silent audio"]


def test_an_untagged_audio_track_is_unknown_and_arr_lid_decides(env, monkeypatch):
    j = probe(["und"], [("eng", "", False)])
    ts = hook.arr_decide.classify(j)
    heard = []
    monkeypatch.setattr(sh, "heard", lambda h, path, index, j, want: heard.append((index, sorted(want))) or None)
    assert sh.verdict(hook, "/d/y.mkv", j, ts, "Japanese", 94, False, "") == [] and heard == [(0, ["jap", "jpn"])]   # no answer passes
    monkeypatch.setattr(sh, "heard", lambda *a: "jpn")
    assert sh.verdict(hook, "/d/y.mkv", j, ts, "Japanese", 94, False, "") == []
    monkeypatch.setattr(sh, "heard", lambda *a: "eng")
    assert sh.verdict(hook, "/d/y.mkv", j, ts, "Japanese", 94, False, "") == ["no Japanese audio, the untagged track sounds like eng"]


# --- runs ----------------------------------------------------------------------------------------

def test_dry_run_makes_one_query_and_changes_nothing(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "probe": ENGLISH}, {"title": NF, "probe": ENGLISH}]
    fake = run(env, monkeypatch)
    assert len(env["hydra"]) == 1 and env["hydra"][0]["t"] == ["movie"] and env["hydra"][0]["imdbid"] == ["0101"]
    assert fake.calls == [] and env["imports"] == [] and env["posts"] == []
    assert not os.path.exists(os.path.join(hook.CFG["STATE_DIR"], "subhunt-radarr.json"))
    (rec,) = lines(env)
    assert rec["outcome"] == "subhunt_dry_run" and rec["source"] == "subhunt" and [c["title"] for c in rec["candidates"]] == [SUBS, NF]
    assert "HYDRAKEY" not in open(hook.CFG["LOG"]).read()


def test_a_release_without_english_subtitles_is_deleted_and_the_next_one_imported(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "probe": NO_ENGLISH}, {"title": NF, "probe": ENGLISH}, {"title": CRIT, "probe": ENGLISH}]
    fake = run(env, monkeypatch, "--apply")
    assert fake.added() == [(SUBS, 0), (NF, -2), (CRIT, -2)]   # every NZB at once, only the first downloads
    (body,) = env["imports"]
    assert body["importMode"] == "copy" and body["files"][0]["movieId"] == 5017 and body["files"][0]["path"].endswith(NF + ".mkv")
    assert body["files"][0]["languages"] == [{"id": 8, "name": "Japanese"}]
    assert fake.deleted() == {"nzo1", "nzo2", "nzo3"} and fake.jobs == {}   # the rejected, the imported and the unused paused job
    assert not (env["tmp"] / "downloads" / SUBS).exists() and not (env["tmp"] / "downloads" / NF).exists()
    assert not os.path.exists(env["keep"])   # the keep link goes once the new file is in place
    st = state(env)
    assert st["status"] == "imported" and st["release"] == NF and [t["title"] for t in st["tried"]] == [SUBS]
    assert st["tried"][0]["why"] == "no full English subtitle (subtitles: por full)" and st["tried"][0]["final"] is True
    assert [r["outcome"] for r in lines(env)] == ["subhunt_queued", "subhunt_rejected", "subhunt_imported"]
    assert [p["title"] for p in env["posts"]] == ["English subtitles found"]


def test_three_failures_keep_the_file_alert_once_and_block_until_forced(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "probe": NO_ENGLISH}, {"title": NF, "plan": "fail:not enough repair blocks"},
                       {"title": CRIT, "probe": SIGNS_ONLY}, {"title": NORD, "probe": ENGLISH}]
    run(env, monkeypatch, "--apply")
    st = state(env)
    assert st["status"] == "no_subbed_release" and env["imports"] == []
    assert [t["why"] for t in st["tried"]] == ["no full English subtitle (subtitles: por full)", "SABnzbd failed the download (not enough repair blocks)",
                                               "no full English subtitle (subtitles: eng forced)"]
    (alert,) = env["posts"]
    assert alert["title"] == "No release with English subtitles" and "Bazarr or OpenSubtitles" in alert["description"]
    assert "not enough repair blocks" in next(f["value"] for f in alert["fields"] if f["name"] == "Tried")
    assert lines(env)[-1]["outcome"] == "no_subbed_release"

    run(env, monkeypatch, "--apply")   # exhausted: no query, no download, no second alert
    assert len(env["hydra"]) == 1 and len(env["posts"]) == 1 and lines(env)[-1]["outcome"] == "subhunt_skipped"

    fake = run(env, monkeypatch, "--apply", "--force")   # forced: a new query, the three tried releases stay out
    assert len(env["hydra"]) == 2 and fake.added() == [(NORD, 0)] and state(env)["status"] == "imported"


def test_a_dead_link_is_deleted_before_anything_downloads(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "plan": "grab"}, {"title": NF, "probe": ENGLISH}]
    fake = run(env, monkeypatch, "--apply")
    dead = next(i for i, q in enumerate(fake.calls) if q.get("name") == "delete" and q["value"] == "nzo1")
    resume = next(i for i, q in enumerate(fake.calls) if q.get("name") == "resume")
    assert dead < resume and state(env)["tried"][0]["why"].startswith("SABnzbd could not fetch the NZB")
    assert state(env)["tried"][0]["final"] is False   # an indexer cap also answers slowly, so --force retries it
    assert state(env)["status"] == "imported"


def test_an_import_that_does_not_land_restores_the_file_keeps_the_download_and_alerts_red(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    env["land"] = False   # Radarr deleted the old file, then the copy failed
    fake = run(env, monkeypatch, "--apply")
    assert open(env["old"], "rb").read() == b"old" and not os.path.exists(env["keep"])   # put back from the keep link
    assert state(env)["status"] == "import_failed" and (env["tmp"] / "downloads" / NF / (NF + ".mkv")).exists()
    assert "nzo1" not in fake.deleted()
    (alert,) = env["posts"]
    assert alert["title"] == "Subtitle hunter import failed" and alert["color"] == hook.COLORS["red"]
    assert "Radarr lists file id 7302" in alert["description"] and "put the current file back" in alert["description"]
    assert lines(env)[-1]["outcome"] == "subhunt_import_failed"

    run(env, monkeypatch, "--apply")   # skipped until forced: no query, no second download, no second alert
    assert len(env["hydra"]) == 1 and len(env["posts"]) == 1 and "The checked download is in" in lines(env)[-1]["result"]
    env["land"] = True
    fake = run(env, monkeypatch, "--apply", "--force")
    assert len(env["hydra"]) == 2 and fake.added() == [(NF, 0)] and state(env)["status"] == "imported"


def test_a_failed_keep_link_blocks_the_import(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    def refuse(src, dst):
        raise OSError("cross-device link")
    monkeypatch.setattr(sh.os, "link", refuse)
    run(env, monkeypatch, "--apply")
    assert env["imports"] == [] and open(env["old"], "rb").read() == b"old" and state(env)["status"] == "import_failed"
    assert "did not import" in env["posts"][0]["description"]


def test_a_file_changed_during_the_download_is_never_replaced(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    real = sh.verdict
    def upgraded(*a):   # Radarr upgrades the movie while the download runs
        env["movie"]["movieFile"] = dict(env["movie"]["movieFile"], id=777)
        return real(*a)
    monkeypatch.setattr(sh, "verdict", upgraded)
    run(env, monkeypatch, "--apply")
    assert env["imports"] == [] and "changed the movie file" in env["posts"][0]["description"]


def test_a_file_with_english_subtitles_costs_no_query(env, monkeypatch):
    env["probes"][env["movie"]["movieFile"]["path"]] = ENGLISH
    run(env, monkeypatch, "--apply")
    assert env["hydra"] == [] and lines(env)[-1]["result"] == "subhunt skipped, the current file has a full English subtitle."


def test_an_error_is_logged_without_keys_and_never_stops_the_run(env, monkeypatch):
    def down(url, *a, **kw):
        raise RuntimeError(f"cannot reach {url}")
    monkeypatch.setattr(hook, "http", down)
    run(env, monkeypatch, "--apply")
    (rec,) = lines(env)
    assert rec["outcome"] == "error" and "apikey=<key>" in rec["result"] and "HYDRAKEY" not in rec["result"]


def test_sonarr_is_refused(env, monkeypatch):
    with pytest.raises(SystemExit):
        sh.main(hook, ["sonarr", "--ids", "1"])


def test_cleanup_deletes_only_the_downloads_own_folder(env, tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "sab", FakeSab({}))
    roots = [str(tmp_path / "movies")]
    lib = tmp_path / "movies" / "Film A"
    assert "kept" in sh.cleanup(hook, CREDS, "n", str(lib), "Film A", roots) and lib.exists()
    shared = tmp_path / "downloads" / "complete"
    shared.mkdir(parents=True)
    assert "kept" in sh.cleanup(hook, CREDS, "n", str(shared), NF, roots) and shared.exists()
    own = shared / NF
    own.mkdir()
    assert sh.cleanup(hook, CREDS, "n", str(own), NF, roots).startswith("deleted") and not own.exists()


def test_creds_come_from_the_apps_database(env, tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "radarr.db")
    db.execute("create table DownloadClients (Name, Implementation, Settings)")
    db.execute("create table Indexers (Name, Implementation, Settings)")
    db.execute("insert into DownloadClients values ('Seedbox', 'RTorrent', '{}')")
    db.execute("insert into DownloadClients values ('SABnzbd', 'Sabnzbd', ?)", (json.dumps({"host": "sab.x", "port": 443, "useSsl": True, "apiKey": "S"}),))
    db.execute("insert into Indexers values ('NZB', 'Newznab', ?)", (json.dumps({"baseUrl": "https://search.x/", "apiPath": "/api", "apiKey": "H"}),))
    db.commit()
    monkeypatch.setitem(hook.CFG, "RADARR_DIR", str(tmp_path))   # radarr.db in RADARR_DIR
    assert REAL_CREDS(hook, "radarr") == {"sab": "https://sab.x:443/api", "sab_key": "S", "hydra": "https://search.x/api", "hydra_key": "H"}


# --- failure paths -------------------------------------------------------------------------------

def test_a_sabnzbd_outage_marks_nothing_and_cleans_up(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "probe": ENGLISH}, {"title": NF, "probe": ENGLISH}, {"title": CRIT, "probe": ENGLISH}]
    def wrap(fake):
        def outage(h, cred, **q):
            if q["mode"] == "addurl" and len(fake.jobs) == 1:
                raise urllib.error.URLError("connection refused")
            return fake(h, cred, **q)
        return outage
    fake = run(env, monkeypatch, "--apply", wrap=wrap)
    assert fake.jobs == {} and fake.deleted() == {"nzo1"}   # the job added before the outage is gone
    assert not os.path.exists(os.path.join(hook.CFG["STATE_DIR"], "subhunt-radarr.json")) and env["posts"] == []
    assert lines(env)[-1]["outcome"] == "error"


def test_a_refused_nzb_is_not_final(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "plan": "refuse"}, {"title": NF, "probe": ENGLISH}]
    run(env, monkeypatch, "--apply")
    (tried,) = state(env)["tried"]
    assert tried["title"] == SUBS and tried["final"] is False and "refused" in tried["why"] and state(env)["status"] == "imported"


def test_a_check_error_rejects_that_candidate_and_the_next_one_runs(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "probe": ENGLISH}, {"title": NF, "probe": ENGLISH}]
    probes = hook.mkvmerge
    def truncated(p):
        if SUBS in p:
            raise RuntimeError("mkvmerge: truncated file")
        return probes(p)
    monkeypatch.setattr(hook, "mkvmerge", truncated)
    run(env, monkeypatch, "--apply")
    tried = state(env)["tried"][0]
    assert tried["title"] == SUBS and tried["final"] is True and tried["why"].startswith("the check failed with RuntimeError")
    assert not (env["tmp"] / "downloads" / SUBS).exists() and state(env)["status"] == "imported"


def test_zero_results_leave_the_movie_open(env, monkeypatch):
    run(env, monkeypatch, "--apply")
    assert lines(env)[-1]["outcome"] == "subhunt_stopped" and env["posts"] == []
    assert not os.path.exists(os.path.join(hook.CFG["STATE_DIR"], "subhunt-radarr.json"))


def test_one_missed_read_keeps_a_good_download(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    reads = []
    def wrap(fake):
        def gap(h, cred, **q):
            if q["mode"] == "history" and "name" not in q:
                reads.append(1)
                if len(reads) == 2:   # the job sits between queue and history for one read
                    return {"history": {"slots": []}}
            return fake(h, cred, **q)
        return gap
    run(env, monkeypatch, "--apply", wrap=wrap)
    assert state(env)["status"] == "imported" and state(env)["tried"] == []


def test_timeouts_leave_the_movie_open_and_force_retries_them(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "plan": "stall"}, {"title": NF, "plan": "stall"}, {"title": CRIT, "plan": "stall"}]
    monkeypatch.setattr(sh, "DOWNLOAD_WAIT", 0)
    fake = run(env, monkeypatch, "--apply")
    st = state(env)
    assert st["status"] == "open" and [t["final"] for t in st["tried"]] == [False] * 3 and env["posts"] == []
    assert lines(env)[-1]["outcome"] == "subhunt_stopped" and fake.jobs == {}
    fake = run(env, monkeypatch, "--apply")   # without --force the three stay out, so nothing is left to try
    assert fake.added() == [] and lines(env)[-1]["outcome"] == "subhunt_stopped"
    fake = run(env, monkeypatch, "--apply", "--force")
    assert [n for n, _ in fake.added()] == [SUBS, NF, CRIT]


def test_a_safety_pause_is_never_resumed(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "probe": ENGLISH, "labels": ["ENCRYPTED"]}, {"title": NF, "probe": ENGLISH, "labels": ["DUPLICATE"]}]
    fake = run(env, monkeypatch, "--apply")
    assert fake.resumed() == ["nzo2"] and state(env)["status"] == "imported"
    tried = state(env)["tried"][0]
    assert tried["why"] == "SABnzbd paused the job as ENCRYPTED" and tried["final"] is True and "nzo1" in fake.deleted()


def test_sigterm_deletes_every_job_and_folder(env, monkeypatch):
    env["releases"] = [{"title": SUBS, "probe": ENGLISH}, {"title": NF, "probe": ENGLISH}]
    def killed(*a):
        raise SystemExit(143)   # what the SIGTERM handler raises
    monkeypatch.setattr(sh, "verdict", killed)
    fakes = []
    with pytest.raises(SystemExit):
        run(env, monkeypatch, "--apply", wrap=lambda fake: fakes.append(fake) or fake)
    assert fakes[0].jobs == {} and fakes[0].deleted() == {"nzo1", "nzo2"}   # the finished job and the paused one
    assert not (env["tmp"] / "downloads" / SUBS).exists() and env["signals"][0][0] == signal.SIGTERM
    with pytest.raises(SystemExit) as ex:
        env["signals"][0][1](signal.SIGTERM, None)
    assert ex.value.code == 143


def test_a_second_apply_run_waits_for_the_first(env, monkeypatch):
    held = hook.try_lock("subhunt.lock")
    with pytest.raises(SystemExit, match="another subtitle hunter run is active"):
        run(env, monkeypatch, "--apply")
    run(env, monkeypatch)   # a dry run writes no state, so it may run beside an apply
    assert lines(env)[-1]["outcome"] == "subhunt_dry_run"
    held.close()


# --- the keep link -------------------------------------------------------------------------------

def test_a_kill_after_the_post_leaves_a_recorded_keep_and_the_next_run_stops_red(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    env["land"] = False   # Radarr deletes the old file, then the copy never lands
    real = hook.arr
    def killed(app, p):
        if p.startswith("command/"):
            raise SystemExit(143)   # SIGTERM while the hunter polls the import command
        return real(app, p)
    monkeypatch.setattr(hook, "arr", killed)
    with pytest.raises(SystemExit):
        run(env, monkeypatch, "--apply")
    assert os.path.exists(env["keep"]) and not os.path.exists(env["old"]) and state(env)["keep"] == env["keep"]

    env["movie"]["movieFile"] = {}   # the movie has no file now, and the keep link is the only copy
    monkeypatch.setattr(hook, "arr", real)
    run(env, monkeypatch, "--apply")
    assert len(env["hydra"]) == 1 and lines(env)[-1]["outcome"] == "subhunt_stopped" and env["keep"] in lines(env)[-1]["result"]
    (alert,) = env["posts"]
    assert alert["color"] == hook.COLORS["red"] and env["keep"] in alert["description"]


def test_an_import_timeout_keeps_the_link_because_radarr_may_still_land_the_copy(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    env["land"] = False
    real = hook.arr
    monkeypatch.setattr(hook, "arr", lambda app, p: {"id": 1, "status": "started"} if p.startswith("command/") else real(app, p))
    monkeypatch.setattr(sh, "IMPORT_WAIT", 0)
    run(env, monkeypatch, "--apply")
    assert os.path.exists(env["keep"]) and not os.path.exists(env["old"])   # nothing moved back under a running import
    assert state(env)["status"] == "import_failed" and state(env)["keep"] == env["keep"]
    (alert,) = env["posts"]
    assert "still importing" in alert["description"] and env["keep"] in alert["description"]


def test_a_failed_remove_after_the_landing_never_moves_the_old_file_back(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    real = os.remove
    def stuck(path):
        if path.endswith(".subhunt-keep"):
            raise PermissionError(path)
        return real(path)
    monkeypatch.setattr(sh.os, "remove", stuck)
    run(env, monkeypatch, "--apply")
    assert state(env)["status"] == "imported" and state(env)["keep"] == env["keep"] and not os.path.exists(env["old"])
    assert "could not be removed" in env["posts"][0]["description"]


def test_a_file_renamed_during_the_download_is_linked_at_its_new_path(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    real = sh.verdict
    def renamed(*a):   # Radarr renames the file while the download runs, with the same file id
        moved = env["old"].replace("WEBDL-1080p", "WEBDL-1080p Proper")
        os.rename(env["old"], moved)
        env["movie"]["movieFile"]["path"] = moved
        env["old"], env["keep"] = moved, os.path.join(os.path.dirname(moved), "." + os.path.basename(moved) + ".subhunt-keep")
        return real(*a)
    monkeypatch.setattr(sh, "verdict", renamed)
    run(env, monkeypatch, "--apply")
    assert len(env["imports"]) == 1 and state(env)["status"] == "imported" and not os.path.exists(env["keep"])


def test_a_timed_out_import_post_keeps_the_link(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    def slow(app, p, method, body=None):   # Radarr may have queued the import before the read timed out
        raise TimeoutError("The read operation timed out")
    monkeypatch.setattr(hook, "arr_write", slow)
    run(env, monkeypatch, "--apply")
    assert os.path.samefile(env["keep"], env["old"]) and state(env)["keep"] == env["keep"] and state(env)["status"] == "import_failed"
    (alert,) = env["posts"]
    assert alert["color"] == hook.COLORS["red"] and "timed out" in alert["description"] and env["keep"] in alert["description"]


def test_a_failed_size_read_records_no_link(env, monkeypatch):
    env["releases"] = [{"title": NF, "probe": ENGLISH}]
    real, reads = os.path.getsize, []
    def unreadable(path):   # find_video() reads the size first. The second read is the one in replace().
        if path.endswith(NF + ".mkv") and len(reads) == 1:
            raise OSError("stale NFS handle")
        reads.append(path)
        return real(path)
    monkeypatch.setattr(sh.os.path, "getsize", unreadable)
    run(env, monkeypatch, "--apply")
    assert "keep" not in state(env) and not os.path.exists(env["keep"]) and env["imports"] == []
    assert state(env)["status"] == "import_failed" and open(env["old"], "rb").read() == b"old"
