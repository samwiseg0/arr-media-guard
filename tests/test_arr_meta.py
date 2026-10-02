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
"""Unit tests for content.py, the metadata checks behind the wrong-content alerts.

The cases follow Radarr alerts of a dry run. Titles, ids, years, runtimes, sizes and durations are made up, and
each case keeps the ratios its checks read. The probes have the shape of mkvmerge -J. No network: TMDB is a fake _get().

Run: pytest tests/test_arr_meta.py
"""
import contextlib
import dataclasses
import fcntl
import http.client
import io
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from arr_media_guard import content as M  # noqa: E402
from arr_media_guard import config, store  # noqa: E402


@pytest.fixture(autouse=True)
def tmdb_up():
    """Every test starts with TMDB up: DOWN is module state."""
    M.DOWN.update(until=0.0, code="", why="", answered=0.0)


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """The TMDB cache and the key alert stamp live in the state store, here in a STATE_DIR of the test."""
    monkeypatch.setattr(config, "CFG", dataclasses.replace(config.CFG, state_dir=str(tmp_path / "state")))
    return tmp_path / "state"


def mkv(header, app, size, tracks=(), attachments=0):
    """A cut-down mkvmerge -J probe. tracks: (type, tag_duration, tag_bps). The statistics app is the writing app."""
    stat = lambda d, b: {"tag_duration": d, "tag_bps": b, "tag__statistics_writing_app": app} if d else {}
    return {"container": {"properties": {"duration": header, "writing_application": app}},
            "tracks": [{"type": t, "properties": stat(d, b)} for t, d, b in tracks],
            "attachments": [{"size": attachments}] if attachments else []}, size


# (probe, size, last video packet) for each case.
PROBES = {
    "Film A": (*mkv(5673794000000, "mkvmerge v4.1.0", 2917802658, [("video", None, None), ("audio", None, None)]), 3901.113),
    "Film B": (*mkv(6179277000000, "mkvmerge v71.1.0", 3866505029, [("video", "01:42:59.231000000", "4175993"),
                                                                    ("audio", "01:42:59.277000000", "821760")]), 6179.192),
    "Film D": (*mkv(56593450000000, "mkvmerge v3.4.0", 4542708124, [("video", None, None), ("audio", None, None)], 1377312), 6943.192),
    "Film E": (*mkv(9017443000000, "mkvmerge v8.9.0", 9241993462, [("video", "02:30:17.420000000", "7525141"),
                                                                   ("audio", "02:30:17.443000000", "672000")]), 9017.381),
    "Film F": (*mkv(7172972000000, "mkvmerge v20.0.0", 4328606025, [("audio", "01:59:32.960000000", "120790"),
                                                                    ("video", "01:59:32.911000000", "4335770"),
                                                                    ("audio", "01:59:32.972000000", "368640")]), 7172.911),
    "Film G": (*mkv(1391069000000, "mkvmerge v10.1.0", 1605172434, [("video", "00:23:11.057000000", "8627710"),
                                                                    ("audio", "00:23:11.069000000", "601600")]), 1391.011),
    "Film H": (*mkv(2316476000000, "mkvmerge v3.4.0", 738365314, [("video", None, None), ("audio", None, None)]), 2316.444),
    "Film J": (*mkv(8233109000000, "mkvmerge v12.0.0", 14686026208, [("video", "02:17:13.105000000", "13867198"),
                                                                     ("audio", "02:17:13.109000000", "400325")], 522913), 8233.065),
    "Film K": (*mkv(4097808000000, "mkvmerge v74.0.0", 5436783199, [("video", "01:08:17.776000000", "9933397"),
                                                                    ("audio", "01:08:17.808000000", "678400")]), 4097.738),
    "Film L": (*mkv(11108748000000, "mkvmerge v83.0", 9357035356, [("video", "02:03:26.147000000", "9482112"),
                                                                   ("audio", "02:03:26.178000000", "620800")]), 7406.104),
    "Film M": (*mkv(5542378000000, "mkvmerge v88.0", 5115555916, [("video", "01:32:22.337000000", "7148915"),
                                                                  ("audio", "01:32:22.378000000", "232960")]), 5542.297),
    "Film N": (*mkv(9529336000000, "mkvmerge v82.0", 7238074150, [("video", "01:40:49.899000000", "8973371"),
                                                                  ("audio", "01:40:49.903000000", "595200")]), 6049.855),
    "Film P": (*mkv(6656566000000, "mkvmerge v89.0", 5253829435, [("video", "01:50:56.434000000", "5614476"),
                                                                  ("audio", "01:50:56.566000000", "697600")]), 6655.48),
    "Film Q": (*mkv(3356638000000, "mkvmerge v58.0.0", 2076503520, [("video", "00:55:56.616000000", "4538985"),
                                                                    ("audio", "00:55:56.601000000", "116656"),
                                                                    ("audio", "00:55:56.582000000", "290950")]), 3356.6),
}


def tmdb(original, spoken, runtime, special=False):
    return {"original": original, "spoken": spoken, "runtime": runtime, "special": special, "unmapped": [], "source": "tmdb movie"}


def other(title, year, runtime, tid):
    return {"title": title, "year": year, "runtime": runtime, "source": f"tmdb movie {tid}", "tmdb": tid}


# label: (audio languages, the app's original, TMDB, listed runtime, release name, year, other years, other film)
CASES = {
    "Film A": (["eng"], "English", tmdb("eng", ["eng", "ger"], 65, True), 65, "", 2003, [], None),
    "Film B": (["spa"], "English", tmdb("eng", ["eng"], 90), 90, "Film.C.2019.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP", 2011, [],
               other("Film C", 2019, 102, 9002)),
    "Film D": (["eng"], "English", tmdb("eng", ["eng", "ger", "ita"], 117), 117, "", 1993, [1994, 1995], None),
    "Film E": (["eng"], "English", tmdb("eng", ["eng", "fre", "spa"], 94), 94, "Film.E.2008.Directors.Cut.3in1.720p.BluRay.DD5.1.x264-GRP",
               2008, [], None),
    "Film F": (["ara", "ara"], "English", tmdb("eng", ["eng"], 117), 117,
               "[Arabic]Film.F.2011.1080p.WEB-DL.DD5.1.H264-GRP", 2012, [2011, 2014], None),
    "Film G": (["eng"], "English", tmdb("eng", ["eng"], 121), 121, "Film.G.2015.1080p.NF.WEB-DL.DDP5.1.H.264-GRP", 2016, [2015],
               other("Film G", 2016, 22, 9006)),
    "Film H": (["eng"], "English", tmdb("eng", ["eng"], 18), 18, "", 2005, [], None),
    "Film J": (["ukr"], "Russian", tmdb("rus", ["pol", "rus", "ukr"], 136), 136, "Film.J.1979.BluRay.720p.FLAC.2.0.x264-GRP",
               1979, [1980, 1983], None),
    "Film K": (["eng"], "English", tmdb("eng", ["eng"], 128), 128, "Film.K.2018.1080p.HULU.WEB-DL.DDP5.1.H.264-GRP", 2017, [],
               other("Film K", 2019, 68, 9009)),
    "Film L": (["eng"], "English", tmdb("eng", ["eng"], 124), 124, "Film.L.1991.1080p.ATVP.WEB-DL.DDP5.1.H.264-GRP", 1991,
               [2007], None),
    "Film M": (["wel"], "English", tmdb("wel", ["eng", "wel"], 92), 92, "Film.M.2019.1080p.NF.WEB-DL.DDP5.1.H.264-GRP",
               2019, [], None),
    "Film N": (["eng"], "English", tmdb("eng", ["eng", "hun", "pol"], 101), 101,
               "Film.N.2002.1080p.DSNP.WEB-DL.DDP5.1.H.264-GRP", 2002, [2003], None),
    "Film P": (["tha"], "English", tmdb("eng", ["eng"], 132), 132, "Film.P.2018.1080p.AMZN.WEB-DL.DDP5.1.x264-GRP", 2018, [2019],
               other("Film P", 2018, 111, 9013)),
    "Film Q": (["eng", "eng"], "English", tmdb("eng", ["eng"], 121, True), 121, "Film.R.Special.1987.1080p.WEB.x264-GRP",
               1987, [], None),
}
WRONG = {"Film B", "Film F", "Film P", "Film G", "Film K", "Film Q"}
# Film Q is a TV movie. A special never scores two on runtime, and nothing else points at it, so it only alerts.
ALERT_ONLY = {"Film Q", "Film H"}


def evidence(label):
    langs, original, exp, listed, release, year, years, oth = CASES[label]
    probe, size, last = PROBES[label]
    tracks = [{"kind": "a", "role": "main", "lang": x, "conf": 0.6} for x in langs]
    alts = [(label, year)] + [(None, y) for y in years]
    return M.wrong_content_evidence(tracks, original, exp, M.trusted_duration(probe, size, last), listed, release,
                                    "special" if exp["special"] else "movie", year, alts, oth)


def test_real_alerts_regrab_only_the_wrong_files():
    got = {label: evidence(label) for label in CASES}
    assert {k for k, v in got.items() if v["regrab"]} == WRONG - ALERT_ONLY, {k: v["why"] for k, v in got.items()}
    # the long-running making-of holds the feature film. One long signal alone only alerts.
    assert {k for k, v in got.items() if v["points"] == 1} == ALERT_ONLY
    assert not any(v["points"] for k, v in got.items() if k not in WRONG | ALERT_ONLY), got


def test_real_alerts_signals():
    kinds = lambda label: sorted(s["kind"] for s in evidence(label)["signals"] if s["points"])
    assert kinds("Film B") == ["language", "other_film", "year"]
    assert kinds("Film F") == ["language", "release_language"]
    assert kinds("Film P") == ["language", "other_film"]
    assert kinds("Film K") == ["other_film", "runtime"]
    assert [s["points"] for s in evidence("Film G")["signals"] if s["kind"] == "runtime"] == [2]
    assert [s["points"] for s in evidence("Film Q")["signals"] if s["kind"] == "runtime"] == [1]


def test_trusted_duration_real_files():
    t = lambda label: M.trusted_duration(*PROBES[label])
    # a header with no second source that agrees: Film D says 15:43:13, Film A 1:34:33 on a 65-minute file
    assert t("Film D")["trust"] == "conflict" and t("Film D")["seconds"] is None
    assert t("Film A")["trust"] == "conflict"
    # a stray subtitle event sets the header of Film L and Film N. Streams, bitrate and last packet agree.
    stray = t("Film L")
    assert (stray["seconds"], stray["trust"], stray["header_ok"], stray["sources"]["header"]) == (7406.178, "bitrate", False, 11108.748)
    assert round(t("Film N")["seconds"]) == 6050
    assert t("Film E")["trust"] == "agree" and t("Film E")["seconds"] == 9017.443
    assert t("Film H")["trust"] == "agree"   # header and last packet, no statistics
    # without the last packet a header alone is never trusted
    probe, size, _ = PROBES["Film D"]
    assert M.trusted_duration(probe, size) == {"seconds": None, "trust": "header", "sources": {"header": 56593.45}, "header_ok": None}


def test_trusted_duration_rules():
    probe, size = mkv(7200 * 10**9, "mkvmerge v92.0", 7200 * 5_000_000 // 8, [("video", "02:00:00.000000000", "4600000"),
                                                                               ("audio", "02:00:00.000000000", "400000")])
    assert M.trusted_duration(probe, size)["trust"] == "agree"
    # statistics written by another app are stale: ffmpeg copies them
    stale = json.loads(json.dumps(probe))
    stale["container"]["properties"]["writing_application"] = "Lavf60.16.100"
    assert M.trusted_duration(stale, size)["trust"] == "header"
    # two pairs that disagree: the header with the last packet against the statistics
    probe, size = mkv(10800 * 10**9, "mkvmerge v92.0", 7200 * 5_000_000 // 8, [("video", "02:00:00.000000000", "4600000"),
                                                                                ("audio", "02:00:00.000000000", "400000")])
    assert M.trusted_duration(probe, size, 10799.0)["trust"] == "conflict"
    # attachments are not bitrate: 400 MB of fonts in a 1.6 GB file
    probe, size = mkv(0, "mkvmerge v92.0", 2_000_000_000, [("video", "00:40:00.000000000", "5000000"),
                                                           ("audio", "00:40:00.000000000", "333334")], attachments=400_000_000)
    assert M.trusted_duration(probe, size)["trust"] == "bitrate"
    # ffprobe of an mp4: the index gives each stream a duration
    ff = {"format": {"duration": "5400.0"}, "streams": [{"codec_type": "video", "duration": "5399.9"}, {"codec_type": "audio", "duration": "5400.0"}]}
    assert M.trusted_duration(ff, 1)["trust"] == "agree"


def test_runtime_verdict():
    tr = lambda minutes: {"seconds": minutes * 60, "trust": "agree"}
    assert M.runtime_verdict(tr(23), 121) == "short" and M.runtime_verdict(tr(150), 94) == "long"
    assert M.runtime_verdict(tr(150), 94, "Film.E.2008.Directors.Cut.720p") == "ok"           # an edition widens to 2.0
    assert M.runtime_verdict(tr(240), 94, "Film.E.2008.Directors.Cut.3in1.720p") == "ok"      # every cut in one file
    assert M.runtime_verdict(tr(210), 100, "Movie.2010.EXTENDED.1080p") == "long"
    assert M.runtime_verdict(tr(95), 65, "", "special") == "ok" and M.runtime_verdict(tr(95), 65) == "long"
    assert M.runtime_verdict(tr(56), 121, "", "special") == "short"
    assert M.runtime_verdict({"seconds": None, "trust": "conflict"}, 117) == "unknown"
    assert M.runtime_verdict(tr(20), 9) == "unknown" and M.runtime_verdict(tr(20), 0) == "unknown"
    # episodes: the short side only, a multi-episode file against its longest episode, a listing under 20 minutes says nothing
    assert M.runtime_verdict(tr(26.5), [30, 30], "", "episode") == "ok"                     # two segments of a 30-minute slot
    assert M.runtime_verdict(tr(88), [44], "", "episode") == "ok"
    assert M.runtime_verdict(tr(21), [43], "", "episode") == "short"
    assert M.runtime_verdict(tr(7), [15], "", "episode") == "unknown"                        # a short cartoon
    assert M.runtime_verdict(tr(40), [44, 0], "", "episode") == "unknown"


def test_year_verdict():
    assert M.year_verdict("Film.C.2019.1080p.AMZN.WEB-DL", 2011, [("Film B", 2011)]) == "mismatch"
    assert M.year_verdict("Film.K.2018.1080p.HULU.WEB-DL", 2017, [("Film K", 2017)]) == "ok"          # one year off
    assert M.year_verdict("Film.G.2015.1080p.NF.WEB-DL", 2016, [("Film G", 2016), (None, 2015)]) == "ok"
    assert M.year_verdict("Film.J.1979.REMASTERED.2019.1080p", 1979, [("Film J", 1979)]) == "ok"
    assert M.year_verdict("1905.1080p.BluRay.x264", 2011, [("1905", 2011)]) == "unknown"
    assert M.year_verdict("Film.S.2044.2013.1080p", 2013, [("Film S 2044", 2013)]) == "ok"
    assert M.year_verdict("2004.Film.T.1971.1080p", 1971, [("2004: Film T", 1971)]) == "ok"
    assert M.year_verdict("Film.S.1978.1080p", 2013, [("Film S 2044", 2013)]) == "mismatch"
    assert M.year_verdict("Show.A.2008.S01E01.720p", 2008, [("Show A", 2008)]) == "ok"
    assert M.year_verdict("Show.A.1979.S01E01.720p", 2008, [("Show A", 2008)]) == "mismatch"
    assert M.year_verdict("Show.B.2025.03.11.Guest.720p", 2003, []) == "unknown"          # an air date
    assert M.year_verdict("7.Show.C.S01E02.Name.1080p", 2011, []) == "unknown"
    assert M.year_verdict("", 2000, []) == "unknown"
    assert M.split_release("[Arabic]Film.F.2011.1080p") == ("Film F", 2011, "[Arabic].1080p")


def test_language_verdict():
    main = lambda *langs: [{"kind": "a", "role": "main", "lang": x, "conf": 0.6} for x in langs]
    assert M.language_verdict(main("wel"), "English", tmdb("wel", ["eng", "wel"], 92))[0] == "ok"        # Film M
    assert M.language_verdict(main("ukr"), "Russian", tmdb("rus", ["pol", "rus", "ukr"], 136))[0] == "ok"  # Film J
    # an English original's spoken list names dubs and real dialogue alike: unknown, never wrong
    assert M.language_verdict(main("ger"), "English", tmdb("eng", ["eng", "ger", "ita"], 117))[0] == "unknown"   # Film D
    assert M.language_verdict(main("jpn"), "English", tmdb("eng", ["eng", "jpn"], 128))[0] == "unknown"          # Film U
    assert M.language_verdict(main("ara"), "English", tmdb("eng", ["eng"], 117))[0] == "wrong"                   # Film F
    # English is always allowed, as in decide(): a foreign original may play its English dub
    assert M.language_verdict(main("eng"), "Italian", tmdb("ita", ["ita"], 124))[0] == "ok"                      # Film V
    assert M.language_verdict(main("ger"), "German", tmdb("eng", ["eng"], 90))[0] == "ok"                # the app's original counts
    assert M.language_verdict(main("por"), "English", None)[0] == "unknown"                              # TMDB did not answer
    assert M.language_verdict(main("und", "por"), "English", tmdb("eng", ["eng"], 90))[0] == "unknown"
    assert M.language_verdict(main("por"), "English", dict(tmdb("eng", ["eng"], 90), unmapped=["xx"]))[0] == "unknown"
    assert M.release_languages("[Arabic]Film.F.2011.1080p") == {"ara"} and M.release_languages("Italian.Film.W.2006.1080p") == set()


def test_episode_language_never_regrabs_alone():
    """Show D S01E02 is German by nature, TMDB lists English only. A language word in the name never adds a point."""
    tracks = [{"kind": "a", "role": "main", "lang": "ger", "conf": 1.0}]
    ev = M.wrong_content_evidence(tracks, "English", tmdb("eng", ["eng"], 0), {"seconds": 2640, "trust": "agree"}, [50],
                                  "Show.D.S01E02.GERMAN.1080p.AMZN.WEB-DL", "episode", 2012, [("Show D", 2012)])
    assert ev["points"] == 1 and not ev["regrab"], ev


def test_listing_in_doubt_cancels_runtime():
    """The app's runtime and TMDB's disagree, so a short file says nothing."""
    tracks = [{"kind": "a", "role": "main", "lang": "eng", "conf": 0.6}]
    ev = M.wrong_content_evidence(tracks, "English", tmdb("eng", ["eng"], 50), {"seconds": 3000, "trust": "agree"}, 120, "", "movie", 2000, [])
    assert ev["points"] == 0 and ev["signals"][1]["verdict"] == "unknown", ev


class FakeTMDB:
    def __init__(self, pages, fail=False):
        self.pages, self.fail, self.calls = pages, fail, []

    def __call__(self, path, token, **params):
        self.calls.append(path)
        if self.fail: raise OSError("network is unreachable")
        return self.pages[path]


FILM_M = {"original_language": "cy", "spoken_languages": [{"iso_639_1": "en"}, {"iso_639_1": "cy"}], "runtime": 92,
          "title": "Film M", "release_date": "2019-08-02", "genres": [{"id": 18}], "keywords": {"keywords": []}}


def test_expected_languages_cache_and_failure(tmp_path, monkeypatch):
    cache = "tmdb"
    fake = FakeTMDB({"/movie/8011": FILM_M})
    monkeypatch.setattr(M, "_get", fake)
    got = M.expected_languages("radarr", {"tmdb": 8011}, "t", cache, now=1000)
    assert {k: got[k] for k in ("original", "spoken", "runtime", "special", "source")} == \
        {"original": "wel", "spoken": ["eng", "wel"], "runtime": 92, "special": False, "source": "tmdb movie 8011"}
    assert M.expected_languages("radarr", {"tmdb": 8011}, "t", cache, now=1000 + 29 * 86400) == got and len(fake.calls) == 1
    M.expected_languages("radarr", {"tmdb": 8011}, "t", cache, now=1000 + 31 * 86400)
    assert len(fake.calls) == 2                                                             # 30 days later it asks again
    monkeypatch.setattr(M, "_get", FakeTMDB({}, fail=True))
    assert M.expected_languages("radarr", {"tmdb": 8008}, "t", cache, now=1000) is None   # a network failure is unknown
    monkeypatch.setattr(M, "radarr_token", lambda: None)
    assert M.expected_languages("radarr", {"tmdb": 8011}, None, cache, now=1000) is None   # no token, no answer


def test_expected_languages_series_by_tvdb(tmp_path, monkeypatch):
    fake = FakeTMDB({"/find/7004": {"tv_results": [{"id": 7005}]},
                     "/tv/7005": {"original_language": "en", "languages": ["en"], "spoken_languages": [{"iso_639_1": "en"}],
                                   "name": "Show D", "first_air_date": "2012-03-14", "genres": [{"id": 99}],
                                   "keywords": {"results": [{"name": "travel"}]}}})
    monkeypatch.setattr(M, "_get", fake)
    got = M.expected_languages("sonarr", {"tvdb": 7004}, "t", "c", now=1)   # no tmdbId: /find
    assert got["original"] == "eng" and got["spoken"] == ["eng"] and got["source"] == "tmdb tv 7005" and got["year"] == 2012
    # the series' own tmdbId comes first, because /find may give another id back
    fake.pages["/tv/1"] = fake.pages["/tv/7005"]
    fake.calls.clear()
    assert M.expected_languages("sonarr", {"tvdb": 7004, "tmdb": 1}, "t", "d", now=1)["source"] == "tmdb tv 1"
    assert fake.calls == ["/tv/1"]


def test_other_film(tmp_path, monkeypatch):
    other_p = {"original_language": "th", "spoken_languages": [{"iso_639_1": "th"}], "runtime": 111, "title": "Film P",
               "release_date": "2018-10-04", "genres": [], "keywords": {"keywords": []}}
    fake = FakeTMDB({"/search/movie": {"results": [{"id": 8013}, {"id": 9013}]}, "/movie/9013": other_p})
    monkeypatch.setattr(M, "_get", fake)
    got = M.other_film("Film.P.2018.1080p.AMZN.WEB-DL.DDP5.1.x264-GRP", 8013, 6656.5, "t", "c", now=1)
    assert got["tmdb"] == 9013 and got["original"] == "tha" and "/movie/8013" not in fake.calls   # the item is never fetched
    assert M.other_film("Film.P.2018.1080p", 8013, 7920, "t", "c", now=1) is None   # no runtime matches
    assert M.other_film("Film A", 8001, 3600, "t", "c", now=1) is None   # no year


def test_radarr_token(tmp_path):
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.sig-_1"
    dll = tmp_path / "Radarr.Common.dll"
    # the real layout of Radarr 6.4: a flag byte and the next string's length byte follow the token. Together they
    # decode as a CJK letter, which a Unicode \w took into the token ("...\u5100Cannot").
    dll.write_bytes(b"\x00junk" + token.encode("utf-16-le") + b"\x00\x51" + "Cannot".encode("utf-16-le"))
    assert M.radarr_token.__wrapped__(str(dll)) == token
    assert M.radarr_token.__wrapped__(str(tmp_path / "missing.dll")) is None


def test_last_packet(monkeypatch):
    class R:
        stdout = json.dumps({"packets": [{"pts_time": "10.5"}, {"pts_time": "12.0"}, {"pts_time": "N/A"}], "format": {"start_time": "1.400000"}})
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **k: R)
    assert M.last_packet("/x.mkv") == 10.6

    def slow(*a, **k):
        raise M.subprocess.TimeoutExpired("ffprobe", 30)
    monkeypatch.setattr(M.subprocess, "run", slow)
    assert M.last_packet("/x.mkv") is None


@pytest.mark.parametrize("code, want", [("cy", "wel"), ("uk", "ukr"), ("th", "tha"), ("pt", "por"), ("zh", "chi"), ("cn", "chi"), ("de", "ger")])
def test_iso_codes(code, want):
    assert M.ISO1[code] == want


def test_very_short_needs_tmdb():
    """Film G runs 23 of 121 minutes. Without TMDB's runtime to confirm the listing it scores one point, not two."""
    tracks = [{"kind": "a", "role": "main", "lang": "eng", "conf": 0.6}]
    ev = M.wrong_content_evidence(tracks, "English", None, M.trusted_duration(*PROBES["Film G"]), 121,
                                  "Film.G.2015.1080p.NF.WEB-DL", "movie", 2016, [("Film G", 2016), (None, 2015)])
    assert ev["points"] == 1 and not ev["regrab"], ev


# Correct files that scored two before a fix.
ENG = [{"kind": "a", "role": "main", "lang": "eng", "conf": 0.6}]
AGREE = lambda minutes: {"seconds": minutes * 60, "trust": "agree", "sources": {"header": minutes * 60}, "header_ok": True}


@pytest.mark.parametrize("tracks, original, exp, minutes, listed, release, year", [
    (ENG, "Italian", tmdb("ita", ["ita"], 124), 124, 124, "Film.V.1968.720p.BluRay.DTS.x264-GRP English", 1968),
    (ENG, "Italian", tmdb("ita", ["ita"], 109), 109, 109, "Film.X.1994.1080p.BluRay.x264-GRP English", 1994),
    (ENG, "Japanese", tmdb("jpn", ["jpn"], 118), 118, 118, "Film.Y.2003.ENGLISH.DUBBED.1080p.BluRay", 2003),
    ([{"kind": "a", "role": "main", "lang": "jpn", "conf": 0.6}], "English", tmdb("eng", ["eng", "jpn"], 128), 128, 128,
     "Film.U.2009.JAPANESE.1080p.BluRay.x264", 2009),
    ([{"kind": "a", "role": "main", "lang": "kor", "conf": 0.6}], "English", tmdb("eng", ["eng", "kor"], 104), 104, 104,
     "Film.Z.2016.KOREAN.1080p.WEB-DL", 2016),
])
def test_correct_foreign_and_dubbed_files_score_zero(tracks, original, exp, minutes, listed, release, year):
    ev = M.wrong_content_evidence(tracks, original, exp, AGREE(minutes), listed, release, "movie", year, [(None, year)])
    assert ev["points"] == 0, ev


def test_a_title_with_its_own_year():
    alts = [("Film AA", 2016), ("Film AA 2016", None)]
    assert M.year_verdict("Film.AA.2016.REMASTERED.2021.1080p", 2016, alts) == "ok"
    assert M.year_verdict("Film.AA.2016.1080p", 2016, alts) == "ok"
    assert M.year_verdict("Film.AA.2009.1080p", 2016, alts) == "mismatch"


def test_an_edition_skips_the_other_film_search(tmp_path, monkeypatch):
    """TMDB lists some cuts as films of their own. An edition word skips the other-film search."""
    monkeypatch.setattr(M, "_get", FakeTMDB({}))   # any call would raise KeyError
    release = "Film.AB.The.Name.Cut.2004.1080p.BluRay.x264"
    assert M.other_film(release, 8020, 112 * 60, "t", "c", now=1) is None
    ev = M.wrong_content_evidence(ENG, "English", tmdb("eng", ["eng"], 121), AGREE(112), 121, release, "movie", 1977, [("Film AB", 1977)])
    assert ev["points"] == 1 and not ev["regrab"], ev   # the year alone
    assert M.EDITION.search("Film.AC.1986.TV.Version.1080p") and M.EDITION.search("Film.AD.1931.Restored.1080p")


def test_other_film_drops_far_years(tmp_path, monkeypatch):
    """TMDB's year matches any release date: "Film AE 2013" returns the 1977 film first."""
    old = {"original_language": "en", "runtime": 104, "title": "Film AE", "release_date": "1977-05-20"}
    new = dict(old, runtime=97, release_date="2013-09-06")
    monkeypatch.setattr(M, "_get", FakeTMDB({"/search/movie": {"results": [{"id": 8030}, {"id": 8031}]}, "/movie/8030": old, "/movie/8031": new}))
    assert M.other_film("Film.AE.2013.1080p", 0, 104 * 60, "t", "c", now=1) is None
    assert M.other_film("Film.AE.2013.1080p", 0, 97 * 60, "t", "c", now=1)["tmdb"] == 8031


def test_special_never_scores_two_on_runtime():
    exp = tmdb("eng", ["eng"], 84, True)
    ev = M.wrong_content_evidence(ENG, "English", exp, AGREE(41), 84, "Comic.Special.2014.1080p", "special", 2014, [("Comic Special", 2014)])
    assert ev["points"] == 1 and not ev["regrab"], ev


def test_header_alert():
    t = lambda label: M.trusted_duration(*PROBES[label])
    assert M.header_alert(t("Film D")) == \
        "The container says 15:43:13, but the file suggests 1:55:43. Neither can be trusted."
    assert M.header_alert(t("Film L")) == "The container says 3:05:08, but the streams run 2:03:26."
    assert M.header_alert(t("Film E")) is None
    probe, size, _ = PROBES["Film D"]
    assert M.header_alert(M.trusted_duration(probe, size)) is None   # a header alone says nothing


class Urlopen:
    """A fake urlopen: an HTTP status to raise, or a JSON body to return. Counts its calls."""
    def __init__(self, status=None, body=None):
        self.status, self.body, self.calls = status, body, 0

    def __call__(self, req, timeout):
        self.calls += 1
        if self.status:
            raise urllib.error.HTTPError(req.full_url, self.status, "status", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps(self.body).encode())


@pytest.mark.parametrize("status, code", [(500, "tmdb_unavailable"), (401, "tmdb_token_rejected"), (403, "tmdb_token_rejected")])
def test_tmdb_failure_is_remembered(tmp_path, monkeypatch, status, code):
    fake = Urlopen(status)
    monkeypatch.setattr(M.urllib.request, "urlopen", fake)
    for tid in (1, 2, 3):   # one request, then a pause of TMDB_RETRY seconds
        assert M.expected_languages("radarr", {"tmdb": tid}, "t", "c") is None
    assert fake.calls == 1 and M.tmdb_state(None)[0] == code
    ev = M.wrong_content_evidence(ENG, "English", None, AGREE(90), 90, "", "movie", 2000, [])
    assert ev["tmdb"] == code and ev["signals"][0]["verdict"] == "unknown"
    M.DOWN["until"] = time.time() - 1   # the pause is over: TMDB is asked again
    M.expected_languages("radarr", {"tmdb": 4}, "t", "c")
    assert fake.calls == 2


def test_tmdb_404_is_an_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(M.urllib.request, "urlopen", Urlopen(404))
    assert M.expected_languages("radarr", {"tmdb": 1}, "t", "c") is None
    assert M.tmdb_state(None)[0] == "no_record" and M.tmdb_state(tmdb("eng", ["eng"], 90))[0] == "found"


def test_tmdb_token_missing_and_unsendable(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "radarr_token", lambda: None)
    assert M.expected_languages("radarr", {"tmdb": 1}, "", "c") is None
    assert M.tmdb_state(None)[0] == "tmdb_token_missing"
    M.DOWN.update(until=0.0)
    # the token with "\u5100Cannot" on its end, as the unfixed regex read it: urllib cannot put it in a header
    assert M.expected_languages("radarr", {"tmdb": 1}, "eyJx.eyJy.z\u5100Cannot", "c") is None
    assert M.tmdb_state(None)[0] == "tmdb_token_rejected"


def test_a_cache_write_the_store_refuses_costs_one_more_request(monkeypatch):
    fake = FakeTMDB({"/movie/8011": FILM_M})
    monkeypatch.setattr(M, "_get", fake)
    real = store.put
    monkeypatch.setattr(store, "put", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    assert M.expected_languages("radarr", {"tmdb": 8011}, "t", "tmdb", now=1)["original"] == "wel"
    assert store.items("tmdb") == {}                                         # nothing cached, and no error
    monkeypatch.setattr(store, "put", real)
    assert M.expected_languages("radarr", {"tmdb": 8011}, "t", "tmdb", now=1)["original"] == "wel"
    assert fake.calls == ["/movie/8011"] * 2 and list(store.items("tmdb")) == ["movie/8011"]


def test_find_fallback_for_a_movie(tmp_path, monkeypatch):
    fake = FakeTMDB({"/find/tt811": {"movie_results": [{"id": 8011}]}, "/movie/8011": FILM_M})
    monkeypatch.setattr(M, "_get", fake)
    assert M.expected_languages("radarr", {"imdb": "tt811"}, "t", "c", now=1)["original"] == "wel"
    fake.pages["/find/tt0"] = {"movie_results": []}
    assert M.expected_languages("radarr", {"imdb": "tt0"}, "t", "c", now=1) is None
    M.expected_languages("radarr", {"imdb": "tt0"}, "t", "c", now=2)
    assert fake.calls.count("/find/tt0") == 1                                # a miss is cached too


def test_the_hook_time_limit_escapes(monkeypatch):
    """A TMDB call or an ffprobe read that the job's time limit cuts raises OutOfTime, and no handler here swallows it.
    A TimeoutError is an OSError and would be. A cut TMDB call never pauses TMDB."""
    clock, asked = [1000.0], []
    monkeypatch.setattr(M.time, "monotonic", lambda: clock[0])
    def hang(*a, timeout, **k):   # the call waits out its timeout
        asked.append(timeout)
        clock[0] += timeout
        raise TimeoutError("timed out")
    monkeypatch.setattr(M.urllib.request, "urlopen", hang)
    for call in (lambda: M.expected_languages("radarr", {"tmdb": 1}, "t", "/nonexistent/c.json", now=1),
                 lambda: M.other_film("Film.P.2018.1080p", 0, 6656, "t", "/nonexistent/c.json", now=1)):
        M.DEADLINE.start(4)
        with pytest.raises(M.OutOfTime, match="^stopped after 4 seconds$"):
            call()
        assert M.DOWN["until"] == 0.0 and M.DEADLINE.end is None
    def slow(argv, timeout, **k):
        asked.append(timeout)
        clock[0] += timeout
        raise M.subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(M.subprocess, "run", slow)
    M.DEADLINE.start(4)
    with pytest.raises(M.OutOfTime):
        M.last_packet("/x.mkv")
    assert M.last_packet("/x.mkv") is None and asked == [4, 4, 4, 30]   # with no limit its own timeout ends the read
    assert not issubclass(M.OutOfTime, OSError)


def test_a_deadline_raises_once_and_ends_its_limit(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(M.time, "monotonic", lambda: clock[0])
    d = M.Deadline()
    assert d.left() is None and d.bound(15) == 15
    d.start(5)
    clock[0] += 2
    assert d.left() == 3 and d.bound(15) == 3 and d.bound(1) == 1
    clock[0] += 3
    with pytest.raises(M.OutOfTime, match="^stopped after 5 seconds$"):
        d.check()
    assert d.end is None and d.left() is None   # a handler that swallows it lets the job run on with no limit
    d = M.Deadline(60, "gave up after waiting 60 seconds for the lock")
    clock[0] += 60
    with pytest.raises(M.OutOfTime, match="^gave up after waiting 60 seconds for the lock$"):
        d.bound(15)


def test_a_paused_deadline_keeps_the_time_it_had_left(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(M.time, "monotonic", lambda: clock[0])
    d = M.Deadline(100)
    clock[0] += 30
    with d.paused() as left:
        assert left == 70 and d.left() is None
        clock[0] += 1000   # a wait with a limit of its own
    assert d.left() == 70
    d.stop()
    with d.paused() as left:
        assert left is None
    assert d.end is None


def test_a_lock_under_a_deadline_gives_up_and_one_with_none_waits(tmp_path):
    """fcntl.flock has no timeout, so a running limit tries with LOCK_NB every LOCK_POLL seconds. key_alert() waits for
    the state store under the job's limit."""
    path = str(tmp_path / "x.lock")
    with open(path, "w") as held, open(path, "w") as f:
        fcntl.flock(held, fcntl.LOCK_EX)
        threading.Timer(2.0, fcntl.flock, (held, fcntl.LOCK_UN)).start()   # a holder that lets go after 2 s
        with pytest.raises(M.OutOfTime, match="^gave up$"):
            M.Deadline(0.3, "gave up").lock(f, fcntl.LOCK_EX)
        M.Deadline().lock(f, fcntl.LOCK_EX)   # no limit: it waits for the holder
        fcntl.flock(f, fcntl.LOCK_UN)
        M.Deadline(5).lock(f, fcntl.LOCK_SH)   # a free lock takes no wait
    store.db()
    with contextlib.closing(sqlite3.connect(store.path(), isolation_level=None)) as other:
        other.execute("BEGIN IMMEDIATE")   # another process writes
        M.DEADLINE.start(0.3)
        started = time.perf_counter()
        with pytest.raises(M.OutOfTime):
            M.key_alert("tmdb_token_rejected", now=1000)
        assert time.perf_counter() - started < 5   # the wait ends at the limit
        other.execute("ROLLBACK")
    assert M.key_alert("tmdb_token_rejected", now=1000)


def test_key_alert_once_a_day(monkeypatch):
    assert M.key_alert("tmdb_unavailable", now=1000) is None      # an outage is no key problem
    title, text = M.key_alert("tmdb_token_rejected", now=1000)
    assert title == "TMDB key not working" and "rejected" in text and "TMDB_TOKEN" in text and "re-grab" in text
    assert M.key_alert("tmdb_token_missing", now=1000 + 86399) is None
    assert "No TMDB key" in M.key_alert("tmdb_token_missing", now=1000 + 86400)[1]
    monkeypatch.setattr(store, "put", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    assert M.key_alert("tmdb_token_missing", now=1000 + 2 * 86400) is None   # no stamp, no alert per file


def test_tmdb_day_status():
    rec = lambda code: {"tmdb": code}
    assert M.tmdb_day_status([rec("found"), rec("no_record"), {}]) == "ok"
    assert M.tmdb_day_status([rec("ok")]) == "ok" and M.tmdb_day_status([rec("ok"), rec("tmdb_token_missing")]) == "key broken"   # a line from before 1.3.0
    assert M.tmdb_day_status([rec("ok"), rec("tmdb_unavailable"), rec("tmdb_unavailable")]) == "unavailable 2 times"
    assert M.tmdb_day_status([rec("tmdb_unavailable"), rec("tmdb_token_rejected")]) == "key broken"
    assert M.tmdb_day_status([{}, {"outcome": "edited"}]) == "no checks"


@pytest.mark.parametrize("error", [http.client.IncompleteRead(b""), http.client.BadStatusLine("<html>"), http.client.LineTooLong("header")])
def test_proxy_garbage_is_unavailable(tmp_path, monkeypatch, error):
    """A proxy or a captive portal answers with something that is not HTTP. That is an outage, never a wrong file."""
    def urlopen(req, timeout):
        raise error
    monkeypatch.setattr(M.urllib.request, "urlopen", urlopen)
    assert M.expected_languages("radarr", {"tmdb": 1}, "t", "c") is None
    assert M.tmdb_state(None)[0] == "tmdb_unavailable"
    M.DOWN.update(until=0.0)
    assert M.other_film("Film.P.2018.1080p", 0, 6656, "t", "c", now=1) is None
    assert M.tmdb_state(None)[0] == "tmdb_unavailable"


def test_answered_counts_live_answers_only(tmp_path, monkeypatch):
    """A cached TMDB answer never counts as a working key: only a live HTTP answer sets DOWN["answered"]."""
    cache = "c"
    monkeypatch.setattr(M.urllib.request, "urlopen", Urlopen(body=FILM_M))
    assert M.expected_languages("radarr", {"tmdb": 8011}, "t", cache, now=1) and M.DOWN["answered"] > 0
    M.DOWN["answered"] = 0.0
    assert M.expected_languages("radarr", {"tmdb": 8011}, "t", cache, now=2)["original"] == "wel"   # from the cache
    assert M.DOWN["answered"] == 0.0
    for status in (404, 401):   # an error status is a live answer too
        monkeypatch.setattr(M.urllib.request, "urlopen", Urlopen(status))
        M.DOWN.update(until=0.0, answered=0.0)
        M.expected_languages("radarr", {"tmdb": status}, "t", cache, now=3)
        assert M.DOWN["answered"] > 0, status
    def refused(req, timeout):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(M.urllib.request, "urlopen", refused)
    M.DOWN.update(until=0.0, answered=0.0)
    M.expected_languages("radarr", {"tmdb": 7}, "t", cache, now=4)
    assert M.DOWN["answered"] == 0.0 and M.tmdb_state(None)[0] == "tmdb_unavailable"


# --- the episode title of a release ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name, title", [
    ("Show.A.S04E15.Ship.Voyage.1080p.WEB.H264-GRP", "Ship Voyage"),
    ("Show.A.S04E15.1080p.WEB.H264-GRP", None),
    ("Show.A.S21.E13.480p.WEBRIP.AAC2.0.x264-GRP", None),
    ("Show.A.S01E01E02.Day.One.720p.HDTV.x264-GRP", "Day One"),
    ("Show A - S02E41 - Night Shift [1080p.WEB-DL] [GRP]", "Night Shift"),
    ("Show.A.E172.Night.Shift.720p.WEBRip.x264-GRP", "Night Shift"),
    ("Show.A.2026.07.17.Slow.Fire.720p.WEB.H264-GRP", "Slow Fire"),
    ("Show.A.S01E01.Night.Shift-GRP", "Night Shift"),   # no quality word: a last "-GRP" is the group
    ("Show.A.S01E01.Night.Shift.REPACK.720p.WEB.H264-GRP", "Night Shift"),   # a release flag ends the title
    ("Show.A.S01E01.Chinese.New.Year.GERMAN.720p.WEB.H264-GRP", "Chinese New Year"),   # a language word ends it in capitals only
    ("Show.A.S02E05.Internal.Affairs.720p.WEB.H264-GRP", "Internal Affairs"),   # a flag in title case is a title word
    ("Show.A.S02E05.Proper.Trouble.720p.WEB.H264-GRP", "Proper Trouble"),
    ("show.a.s02e05.night.shift.internal.720p.web.h264-grp", "night shift"),
    ("SHOW.A.S02E05.FRENCH.WEEK.720P.HDTV.X264-GRP", "FRENCH WEEK"),   # all in capitals: only the tags at the end are cut
    ("SHOW.A.S02E05.NIGHT.SHIFT.FRENCH.REPACK.720P.HDTV.X264-GRP", "NIGHT SHIFT"),
    ("Show.A.S04E15b.Night.Shift.DVDRip.x264-GRP", "Night Shift"),   # one segment of a DVD order
    ("Show.A.2018.S04E12.The.Crossing.1080p.AMZN.WEB-DL", "The Crossing"),   # the year before the tag is no date
])
def test_the_release_name_gives_the_title_between_the_tag_and_the_quality(name, title):
    assert M.release_episode_title(name) == title


def test_the_nfo_gives_the_episode_title_line_first():
    assert M.nfo_episode_title("Title.........: Ship Voyage/No Lady Here\nSize : 1 GB\n") == "Ship Voyage/No Lady Here"
    assert M.nfo_episode_title(" Title    : Show A\n Episode Title : Night Shift\n") == "Night Shift"
    assert M.nfo_episode_title("Title: Show A S04E15 Night Shift\n") == "Night Shift"
    assert M.nfo_episode_title("Release: Show.A.S04E15\nRuntime: 11 min\n") is None


def test_title_keys_ignore_case_marks_and_the_series_name():
    assert M.title_key("That's No Lady & Co.!") == "thats no lady and co" and M.title_key("Café Noël") == "cafe noel"
    assert M.title_keys("Ship Voyage / No Lady Here", {"show a"}) == {"ship voyage no lady here", "ship voyage", "no lady here"}
    assert M.title_keys("Show A Night Shift", {"show a"}) == {"night shift"} and M.title_keys("Show A", {"show a"}) == set()


def ep(i, season, number, title, absolute=None):
    return {"id": i, "seasonNumber": season, "episodeNumber": number, "title": title, "absoluteEpisodeNumber": absolute}


SERIES = [ep(1, 4, 15, "A Rainy Day"), ep(2, 4, 21, "Ship Voyage"), ep(3, 4, 22, "No Lady Here"), ep(4, 1, 1, "Reunion Part 2"),
          ep(5, 2, 1, "The Reunion Part 2"), ep(6, 3, 1, "Homecoming"), ep(7, 3, 2, "Homecoming"), ep(8, 0, 4, "Behind the Scenes"),
          ep(9, 3, 25, "Robot Rescue"), ep(10, 3, 45, "Two Pups"), ep(11, 3, 46, "Three Pups")]


@pytest.mark.parametrize("title, imported, release, verdict, found", [
    ("Ship Voyage/No Lady Here", [1], "Show.A.S04E15.1080p", "other", [2, 3]),   # each segment matches on its own
    ("Ship Voyage", [1], "Show.A.S04E15.Ship.Voyage.1080p", "other", [2]),
    ("A Rainy Day", [1], "Show.A.S04E15.A.Rainy.Day.1080p", "imported", []),
    ("Ship Voyage/A Rainy Day", [1], "Show.A.S04E15.1080p", "imported", []),   # one segment is the file's own
    ("The Reunion Part 2", [4], "Show.A.S01E01.The.Reunion.Part.2.1080p", "imported", []),   # near the file's own title
    ("Homecoming", [1], "Show.A.S04E15.Homecoming.1080p", "none", []),   # two episodes share it, so it names neither
    ("Behind the Scenes", [1], "Show.A.S04E15.Behind.the.Scenes.1080p", "none", []),   # a special counts for a special only
    ("A Rainy Day", [8], "Show.A.S00E04.A.Rainy.Day.1080p", "other", [1]),
    ("Robot Rescue", [10, 11], "Show.A.S03E25.Robot.Rescue.1080p", "mapped", []),   # Sonarr mapped S03E25 to E45-E46
    ("Robot Rescue", [10, 11], "Show.A.S03E45E46.Robot.Rescue.1080p", "other", [9]),   # a two-episode file
    ("Nothing Like It", [1], "Show.A.S04E15.Nothing.Like.It.1080p", "none", []),
    ("Ship Voyage and No Lady Here", [1], "Show.A.S04E15.Ship.Voyage.and.No.Lady.Here.1080p", "other", [2, 3]),   # two titles, no "/"
    ("Ship Voyage No Lady Here", [1], "Show.A.S04E15.Ship.Voyage.No.Lady.Here.1080p", "other", [2, 3]),
    ("And Ship Voyage", [1], "Show.A.S04E15.And.Ship.Voyage.1080p", "none", []),   # a connector never starts a title
    ("Ship Voyage and", [1], "Show.A.S04E15.Ship.Voyage.and.1080p", "none", []),
    ("Rainy", [1], "Show.A.S04E15.Rainy.1080p", "imported", []),   # words of the file's own title in a row, far under TITLE_NEAR
])
def test_an_episode_title_names_another_episode_only_when_no_imported_one_matches(title, imported, release, verdict, found):
    v = M.episode_title_verdict(title, SERIES, imported, release, ["Show A"])
    assert (v["verdict"], v["episodes"], v["points"]) == (verdict, found, 0), v


@pytest.mark.parametrize("title, own", [("Versus the Ring", "Versus the Ring Part 2"), ("The Claw (1)", "The Claw (2)")])
def test_two_parts_of_one_story_never_name_each_other(title, own):
    """A swap of part 1 and part 2 shows, though the titles are near and one holds the other's words."""
    eps = [ep(1, 2, 22, title), ep(2, 2, 23, own)]
    v = M.episode_title_verdict(title, eps, [2], "Show.A.S02E23.Title.720p")
    assert (v["verdict"], v["names"]) == ("other", "S02E22"), v


def test_the_verdict_names_both_orders_and_the_absolute_number():
    v = M.episode_title_verdict("Ship Voyage", SERIES, [1], "Show.A.S04E15.1080p", said="the release's NFO")
    assert v["why"] == "the release's NFO says Ship Voyage, which Sonarr lists as S04E21. It was imported as S04E15", v
    v = M.episode_title_verdict("Ship Voyage/No Lady Here", SERIES, [1], "Show.A.S04E15.1080p")
    assert v["names"] == "S04E21 and S04E22" and v["why"].startswith("the release's title says"), v   # the advice names both
    v = M.episode_title_verdict("Ship Voyage/No Lady Here/Robot Rescue", SERIES, [1], "Show.A.S04E15.1080p")
    assert v["names"] == "S03E25, S04E21 and S04E22", v
    assert M.episode_title_verdict("Ship Voyage", SERIES, [], "Show.A.S04E15.1080p") is None   # no episode points to the file
    anime = [ep(1, 5, 10, "First Light", 120), ep(2, 5, 20, "Last Light", 130)]
    v = M.episode_title_verdict("Last Light", anime, [1], "Show.B.E120.Last.Light.720p", anime=True)   # an absolute number
    assert v["verdict"] == "other" and v["why"].endswith("lists as S05E20 (absolute 130). It was imported as S05E10 (absolute 120)"), v
    assert M.episode_title_verdict("Last Light", anime, [1], "Show.B.E125.Last.Light.720p")["verdict"] == "mapped"
    assert "absolute" not in M.episode_title_verdict("Last Light", anime, [1], "Show.B.E120.Last.Light.720p")["why"]   # a standard series


def test_an_episode_title_never_adds_a_point():
    """The title alerts only. With a short runtime, its signal still leaves the evidence at one point, no re-grab."""
    tracks = [{"kind": "a", "role": "main", "lang": "eng", "conf": 1.0}]
    sig = M.episode_title_verdict("Ship Voyage", SERIES, [1], "Show.A.S04E15.Ship.Voyage.1080p")
    ev = M.wrong_content_evidence(tracks, "English", tmdb("eng", ["eng"], 0), {"seconds": 600, "trust": "agree"}, [50],
                                  "Show.A.S04E15.Ship.Voyage.1080p", "episode", 2012, [("Show A", 2012)], episode=sig)
    assert ev["signals"][-1] is sig and ev["points"] == 1 and not ev["regrab"] and "Ship Voyage" not in ev["why"], ev



# --- the tester's case: one interactive search for S04E15 of a cartoon in three episode orders ---------------------------

SPONGE = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "episode_title", "spongebob-s00-s04-episodes.json")))
GHOST_HOST = 133   # S04E15 in Sonarr's order. The releases below were found for it.


@pytest.mark.parametrize("name, want", [
    ('SpongeBob.S04E15.PDTV.HebSub.XviD.D00oo00M', None),
    ('spongebob.schwammkopf.s04e15.die.fanthaddaeustische.reise.patricia.german.dl.fs.1080p.web.h264-cnhd', 'none'),
    ('SpongeBob.Schwammkopf.S04E15.Die.fanthaddaeustische.Reise.-.Patricia.GERMAN.DL.FS.1080p.WEB.H264-CNHD', 'none'),   # a German title names no episode
    ('Spongebob.Schwammkopf.S04E15.German.AC3.DL.1080p.WebRip.x265-FuN', 'none'),
    ('SpongeBob.SquarePants.1999.S04E15.FRENCH.1080p.WEB.H264-FTMVHD', None),
    ('SpongeBob.SquarePants.1999.S04E15.Ghost.Host.REPACK.1080p.AMZN.Webrip.x265.10bit.EAC3.2.0-Frys-TAoE', 'imported'),
    ("SpongeBob.SquarePants.2005.S04E15.Squidtastic.Voyage.That's.No.Lady.1080p.PMTP.WEB-DL.AAC.SDR.H.264-LioN", ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.1080p.AMZN.WEB-DL.DD2.0.H.264.1-AME', None),
    ('SpongeBob.SquarePants.S04E15.1080p.AMZN.WEB-DL.DD2.0.H.264-AME', None),
    ('SpongeBob.SquarePants.S04E15.1080p.AMZN.WEB-DL.DD2.0.H264-AME', None),
    ('SpongeBob.SquarePants.S04E15.1080p.AMZN.WEB-DL.DD2.0.H264-BTN', None),
    ('SpongeBob.SquarePants.S04E15.1080p.AMZN.WEB-DL.DD2.0.H.264-PlayWEB', None),
    ('SpongeBob.SquarePants.S04E15.1080p.AMZN.WEB-DL.DDP2.0.H.264-Kitsune', None),
    ('Spongebob.Squarepants.S04E15.1080p.AMZN.WEB-DL.DDP2.0.H.264-SiGLA', None),
    ('SpongeBob.SquarePants.S04E15.1080p.AV1.10bit-MeGusta', None),
    ('SpongeBob.SquarePants.S04E15.1080p.HEVC.x265-MeGusta', None),
    ('SpongeBob.SquarePants.S04E15.1080p.SKST.WEB-DL.DD2.0.H.264-PlayWEB', None),
    ('SpongeBob.SquarePants.S04E15.1080p.WEB-DL.HebSub.x264-D00oo00M', None),
    ('SpongeBob.SquarePants.S04E15.1080p.WEB.H264-OUTPOST31', None),
    ('SpongeBob.SquarePants.S04E15.720p.AMZN.WEB-DL.DDP2.0.H.264-VARYG', None),
    ('SpongeBob.SquarePants.S04E15.720p.HEVC.x265-MeGusta', None),
    ('SpongeBob.SquarePants.S04E15a-All.that.Glitters', ('other', 'S04E19')),   # a DVD segment, and no quality word
    ('SpongeBob.SquarePants.S04E15a.DVDRip.iNTERNAL.XviD-iND', None),
    ('SpongeBob.SquarePants.S04E15a.Squidtastic.Voyage.DVDRip.x264-TiMEGOD', ('other', 'S04E23')),
    ('SpongeBob.SquarePants.S04E15b.DVDRip.iNTERNAL.XviD-iND', None),
    ("SpongeBob.SquarePants.S04E15b.That's.No.Lady.DVDRip.x264-TiMEGOD", ('other', 'S04E27')),
    ('SpongeBob.SquarePants.S04E15b.Thats.No.Lady.DVDRip.x264-TiMEGOD', ('other', 'S04E27')),
    ('Spongebob.Squarepants.S04E15.Bummer.Vacation.Wigstuck.1080p.JHS.WEB-DL.English.AAC.2.0.H264-Rv', 'none'),   # "Wigstuck" is a typo, so no title covers it
    ('SpongeBob.SquarePants.S04E15b-Wishing.You.Well', ('other', 'S04E20')),
    ('SpongeBob.SquarePants.S04E15.DUTCH.1080p.WEB.h264-NLKIDS', None),
    ('SpongeBob.SquarePants.S04E15-E16.Ghost.Host..Chimps.Ahoy.1080p.PMTP.WEB-DL.AAC2.0.x264-AndreMor', 'imported'),
    ('SpongeBob.SquarePants.S04E15E16.Ghost.Host..Chimps.Ahoy.MULTI.1080p.MAX.WEB-DL.DDP2.0.H.264-AndreMor', 'imported'),   # one segment is the file's own
    ('SpongeBob.SquarePants.S04E15E16.Ghost.Host..Chimps.Ahoy.MULTI.1080p.SKST.WEB-DL.DDP2.0.x264-AndreMor', 'imported'),
    ('SpongeBob.SquarePants.S04E15-E16.Krusty.Towers.and.Mrs.Puff.Youre.Fired.1080p.AMZN.WEB-DL.AAC2.0.h.264-CHX', ('other', 'S04E13 and S04E14')),   # a third order
    ('SpongeBob.SquarePants.S04E15.FRENCH.1080p.WEB.H264-HEADER', None),
    ('SpongeBob.SquarePants-S04E15-Ghost.Host', 'imported'),   # dashes, and no quality word
    ('SpongeBob.SquarePants.S04E15.Ghost.Host.1080p.AMZN.WEBRip.DDP.2.0.H.265-iVy', 'imported'),
    ('SpongeBob.SquarePants.S04E15.Ghost.Host.1080p.AMZN.WEBRip.DDP2.0.x264', 'imported'),
    ('Spongebob.Squarepants-S04e15-Ghost.Host-19', 'imported'),
    ('SpongeBob SquarePants - S04E15 - Ghost Host - DVD', 'imported'),
    ('SpongeBob.SquarePants.S04E15.Ghost.Host.REPACK.1080p.AMZN.WEB-DL.DDP2.0.H.264-Kitsune', 'imported'),
    ('SpongeBob.SquarePants.S04E15.Ghost.Host.REPACK.1080p.AMZN.Webrip.x265.10bit.EAC3.2.0.-.Frys.TAoE', 'imported'),
    ('SpongeBob.SquarePants.S04E15.NORDiC.ENG.1080p.SKST.WEB-DL.H.264-NORViNE', None),
    ('SpongeBob.SquarePants.S04E15.REPACK.1080p.AMZN.WEB-DL.DDP2.0.H.264-Kitsune', None),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.amp;amp;amp;amp;.Thats.No.Lady.1080p.SKST.WEB-DL.DD+2.0.H.264-playWEB', ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.amp;amp;amp;.Thats.No.Lady.1080p.SKST.WEB-DL.DD+2.0.H.264-playWEB', ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.amp;amp;.Thats.No.Lady.1080p.SKST.WEB-DL.DD+2.0.H.264-playWEB', ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.amp;.Thats.No.Lady.1080p.SKST.WEB-DL.DD+2.0.H.264-playWEB', ('other', 'S04E23 and S04E27')),   # an escaped "&" leaves "amp;"
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.and.Thats.No.Lady.1080p.SKST.WEB-DL.DD+2.0.H.264-playWEB', ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.-.Thats.No.Lady.1080p.HMAX.WEB-DL.DDP2.0.H.265.DUAL-Potatin', ('other', 'S04E23 and S04E27')),   # "Thats" matches "That's"
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.-.Thats.No.Lady.1080p.PMTP.WEB-DL.AAC2.0.H.264.DUAL-OLYMPUS', ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage..Thats.No.Lady.1080p.SKST.WEB-DL.DD+2.0.H.264-playWEB', ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.Thats.No.Lady.1080p.SKST.WEB-DL.DDP2.0.H.264-OldT', ('other', 'S04E23 and S04E27')),
    ('SpongeBob.SquarePants.S04E15.Squidtastic.Voyage.Thats.No.Lady.720p.SKST.WEB-DL.DDP2.0.H.264-OldT', ('other', 'S04E23 and S04E27')),
    ('Spongebob Squarepants S04E15 TVRip HebDub XviD', None),
    ('Spongebob.Squarepants.S04E15.TVRip.HebDub.XviD', None),
    ('Spongebob.Squarepants.S04E15.TVRip.HebDub.XviD-P2P', None),
    ('Spongebob.Squarepants.S04E15.TVRip.HebDub.XviD-P2P-Rakuv01', None),
    ('Spongebob.Squarepants.S04E15.TVRip.HebDub.XviD-Rakuvfinhel', None),
    ('SpongeBob.SquarePants.S04E15.XviD-AFG', None),
    ('SpongeBob.Squarepants.SD.S04E15', None),
])
def test_each_release_of_one_search_gets_its_verdict(name, want):
    """None: the name gives no title. Otherwise the verdict, with the episodes it names when it is "other"."""
    title = M.release_episode_title(name)
    v = title and M.episode_title_verdict(title, SPONGE, [GHOST_HOST], name, ["SpongeBob SquarePants"])
    assert (None if not v else (v["verdict"], v["names"]) if v["verdict"] == "other" else v["verdict"]) == want, (title, v)
