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
"""Unit tests for the env file settings of arr-media-guard: their defaults, their values, the removed keys and the
values the script cannot read. Each test loads the script by path with its own env file.

Run: pytest tests/test_settings.py
"""
import importlib.machinery
import importlib.util
import itertools
import os
import sqlite3

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
POLICY = os.path.abspath(os.path.join(FILES, "examples", "policy.json"))
COUNT = itertools.count()


def load(tmp_path, text=""):
    """The script, loaded with an env file that holds text and the example policy."""
    env = tmp_path / f"env{next(COUNT)}"
    env.write_text(f"POLICY_FILE='{POLICY}'\n" + text)
    old = os.environ.get("ARR_MEDIA_GUARD_ENV")
    os.environ["ARR_MEDIA_GUARD_ENV"], os.environ["ARR_MEDIA_GUARD_LIB"] = str(env), FILES
    try:
        name = f"arr_media_guard_settings{next(COUNT)}"
        loader = importlib.machinery.SourceFileLoader(name, os.path.join(FILES, "arr-media-guard"))
        m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(m)
        return m
    finally:
        os.environ["ARR_MEDIA_GUARD_ENV"] = old if old is not None else "/nonexistent/arr-media-guard.env"


def test_each_app_reads_config_xml_and_its_database_in_its_folder(tmp_path):
    m = load(tmp_path)
    assert (m.app_dir("radarr"), m.app_dir("sonarr")) == ("/var/lib/radarr", "/var/lib/sonarr")
    folder = tmp_path / "sonarr"
    folder.mkdir()
    (folder / "config.xml").write_text("<Config><ApiKey>k3y</ApiKey></Config>")
    con = sqlite3.connect(folder / "sonarr.db")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (7)")
    con.commit()
    con.close()
    m = load(tmp_path, f"SONARR_DIR='{folder}'\n")
    assert m.app_dir("sonarr") == str(folder) and m.app_dir("radarr") == "/var/lib/radarr"
    assert m.api_key("sonarr") == "k3y" and m.app_db("sonarr", "SELECT x FROM t", ()) == [(7,)]
    assert load(tmp_path, f"SONARR_DIR='{folder}'\nSONARR_API_KEY='fromenv'\n").api_key("sonarr") == "fromenv"


def test_selftest_names_a_removed_key_and_passes(tmp_path, capsys):
    m = load(tmp_path, "RADARR_CONFIG='/x/config.xml'\nRADARR_DB='/x/radarr.db'\nSONARR_CONFIG='/y'\nVIDEO_REGRAB_CAP='5'\n")
    m.main(["--selftest"])
    out = capsys.readouterr().out
    for k, new in (("RADARR_CONFIG", "RADARR_DIR"), ("RADARR_DB", "RADARR_DIR"), ("SONARR_CONFIG", "SONARR_DIR"), ("VIDEO_REGRAB_CAP", "REGRAB_CAP")):
        assert f"unused key {k} in " in out and f"{new} replaced it" in out
    assert out.rstrip().endswith("selftest ok") and m.app_dir("radarr") == "/var/lib/radarr"   # the old key changes nothing
    load(tmp_path).main(["--selftest"])
    assert "unused key" not in capsys.readouterr().out


@pytest.mark.parametrize("text, kinds", [("", {"audio", "video"}), ("REGRAB='audio, Content ,DAMAGE'\n", {"audio", "content", "damage"}),
                                         ("REGRAB='none'\n", set()), ("REGRAB=' None '\n", set()), ("REGRAB='video,video'\n", {"video"})])
def test_regrab_lists_the_kinds_that_regrab(tmp_path, text, kinds):
    m = load(tmp_path, text)
    assert m.REGRAB == kinds and m.CONFIG_ERRORS == []


def test_an_unknown_regrab_kind_is_left_out_and_fails_the_selftest(tmp_path, capsys):
    m = load(tmp_path, "REGRAB='audio,sound,video'\nWRONG_CONTENT_REGRAB='true'\nDAMAGE_REGRAB='true'\n")
    assert m.REGRAB == {"audio", "video"}   # the safest reading: only the kinds it knows
    assert m.CONFIG_ERRORS == ["REGRAB names sound, which is no re-grab kind, so it is left out. The kinds are audio, video, content, damage."]
    with pytest.raises(SystemExit) as ex:
        m.main(["--selftest"])
    assert "REGRAB names sound" in str(ex.value)
    out = capsys.readouterr().out
    assert "unused key WRONG_CONTENT_REGRAB in " in out and "unused key DAMAGE_REGRAB in " in out   # the old switches do nothing


def test_name_names_the_hidden_folders(tmp_path, capsys):
    m = load(tmp_path)
    assert (m.KEEP_DIR, m.HIDE_DIR, m.RECYCLE_DIR) == (".arr-media-guard-originals", ".arr-media-guard-convert", ".arr-media-guard-recycle")
    assert m.CONFIG_ERRORS == []
    m = load(tmp_path, "NAME='site-guard.2'\nKEEP_DIR='.kept'\nHIDE_DIR='.hidden'\n")
    assert (m.KEEP_DIR, m.HIDE_DIR, m.RECYCLE_DIR, m.CFG["NAME"]) == (".site-guard.2-originals", ".site-guard.2-convert", ".site-guard.2-recycle",
                                                                     "site-guard.2")
    m.main(["--selftest"])   # the old keys change nothing, and the selftest names them
    out = capsys.readouterr().out
    assert "unused key KEEP_DIR in " in out and "unused key HIDE_DIR in " in out and "NAME replaced it" in out


@pytest.mark.parametrize("name", ["", "a/b", "has space", "../x"])
def test_a_name_that_is_no_plain_folder_name_takes_the_default_and_fails_the_selftest(tmp_path, name):
    m = load(tmp_path, f"NAME='{name}'\n")
    assert (m.KEEP_DIR, m.HIDE_DIR, m.RECYCLE_DIR, m.CFG["NAME"]) == (".arr-media-guard-originals", ".arr-media-guard-convert",
                                                                     ".arr-media-guard-recycle", "arr-media-guard")
    assert len(m.CONFIG_ERRORS) == 1 and m.CONFIG_ERRORS[0].startswith(f"NAME {name!r} holds other characters")
    with pytest.raises(SystemExit, match="NAME"):
        m.main(["--selftest"])


@pytest.mark.parametrize("text, level", [("", "fix"), ("SUBTITLES='off'\n", "off"), ("SUBTITLES='Check'\n", "check"),
                                         ("SUBTITLES='deep'\n", "deep")])
def test_subtitles_sets_the_level_of_an_import(tmp_path, text, level):
    m = load(tmp_path, text)
    assert m.SUBTITLES == level and m.CONFIG_ERRORS == []


def test_an_unknown_subtitles_level_acts_as_check_and_fails_the_selftest(tmp_path, capsys):
    m = load(tmp_path, "SUBTITLES='on'\nSUB_CHECK='false'\nSUB_TIMING='false'\nSUB_DEEP_ANALYSIS='true'\n")
    assert m.SUBTITLES == "check" and m.CONFIG_ERRORS == [
        "SUBTITLES 'on' is no level, so the subtitle check of an import runs as check. The levels are off, check, fix, deep."]
    with pytest.raises(SystemExit, match="SUBTITLES 'on' is no level"):
        m.main(["--selftest"])
    out = capsys.readouterr().out
    assert all(f"unused key {k} in " in out for k in ("SUB_CHECK", "SUB_TIMING", "SUB_DEEP_ANALYSIS"))


@pytest.mark.parametrize("text, kinds, error", [("REGRAB=''\n", set(), "REGRAB is blank, so nothing re-grabs. Write REGRAB='none'"),
                                                ("REGRAB=' , '\n", set(), "REGRAB is blank"),
                                                ("REGRAB='none,audio'\n", {"audio"}, "REGRAB names none, which is no re-grab kind")])
def test_a_blank_regrab_re_grabs_nothing_and_fails_the_selftest(tmp_path, text, kinds, error):
    m = load(tmp_path, text)
    assert m.REGRAB == kinds and len(m.CONFIG_ERRORS) == 1 and m.CONFIG_ERRORS[0].startswith(error), m.CONFIG_ERRORS
    with pytest.raises(SystemExit, match="REGRAB"):
        m.main(["--selftest"])


@pytest.mark.parametrize("value, pairs", [("", []), ("/tv:/media/tv|/movies:/m", [("/tv", "/media/tv"), ("/movies", "/m")]),
                                          ("/tv:/media/tv|/a:/b:/c", [("/tv", "/media/tv")]), ("tv:/media/tv", []), ("/tv=/media/tv", []),
                                          ("/tv:", [])])
def test_a_path_map_pair_that_is_not_two_absolute_paths_is_left_out_and_fails_the_selftest(tmp_path, value, pairs):
    m = load(tmp_path, f"PATH_MAP='{value}'\n")
    bad = len(pairs) != len([p for p in value.split("|") if p])
    assert m.PATH_MAP == pairs and bool(m.PATH_MAP_ERROR) == bad and (m.CONFIG_ERRORS == [m.PATH_MAP_ERROR] if bad else m.CONFIG_ERRORS == [])
    if bad:
        with pytest.raises(SystemExit, match="PATH_MAP takes pairs"):
            m.main(["--selftest"])


@pytest.mark.parametrize("quote", ["'", '"', ""])
def test_each_program_map_reads_paths_with_spaces_and_an_empty_one_takes_path_map(tmp_path, quote):
    m = load(tmp_path, f"PATH_MAP={quote}/data:/media{quote}\nSONARR_PATH_MAP={quote}/mnt/TV:/media/TV|/mnt/Anime:/media/Anime{quote}\n"
                       f"RADARR_PATH_MAP={quote}{quote}\nPLEX_PATH_MAP={quote}/mnt/TV Shows:/media/TV{quote}\n")
    assert m.MAPS == {"sonarr": [("/mnt/TV", "/media/TV"), ("/mnt/Anime", "/media/Anime")], "plex": [("/mnt/TV Shows", "/media/TV")]}
    assert m.PATH_MAP == [("/data", "/media")] and m.CONFIG_ERRORS == [] and m.PATH_MAP_ERROR is None
    assert m.mapped("/media/TV/A/a.mkv", "plex", True) == "/mnt/TV Shows/A/a.mkv" and m.mapped("/data/a.mkv", "radarr") == "/media/a.mkv"
    assert (m.map_key("sonarr"), m.map_key("radarr")) == ("SONARR_PATH_MAP", "PATH_MAP")


def test_the_docker_env_file_keeps_every_map_on_path_map_until_one_is_set(tmp_path):
    """The image writes the example env file, then the Docker section. A line added after them wins."""
    text = "".join(open(os.path.join(FILES, *f)).read() for f in (("examples", "arr-media-guard.env"), ("docker", "arr-media-guard.env")))
    m = load(tmp_path, text)
    assert (m.MAPS, m.PATH_MAP, m.CONFIG_ERRORS, m.KEEP_REPLACED) == ({}, [], [], False)
    m = load(tmp_path, text + "PATH_MAP='/tv:/media/TV'\nRADARR_PATH_MAP='/movies:/media/Movies'\nPLEX_PATH_MAP='/mnt/Movies:/media/Movies'\n")
    assert m.MAPS == {"radarr": [("/movies", "/media/Movies")], "plex": [("/mnt/Movies", "/media/Movies")]} and m.CONFIG_ERRORS == []


@pytest.mark.parametrize("key", ["SONARR_PATH_MAP", "RADARR_PATH_MAP", "PLEX_PATH_MAP"])
def test_a_bad_pair_in_a_program_map_fails_the_selftest_and_stops_the_listener(tmp_path, key):
    m = load(tmp_path, f"PATH_MAP='/tv=/media/tv'\n{key}='/a:/b|/c:d'\n")
    assert m.MAPS[key.split("_")[0].lower()] == [("/a", "/b")]
    assert m.CONFIG_ERRORS == ["PATH_MAP takes pairs APP_PATH:LOCAL_PATH of absolute paths, joined by '|'. A pair that is not one is left out.",
                               f"{key} takes pairs {'PLEX' if key == 'PLEX_PATH_MAP' else 'APP'}_PATH:LOCAL_PATH of absolute paths, joined by '|'. "
                               "A pair that is not one is left out."]
    assert m.PATH_MAP_ERROR == " ".join(m.CONFIG_ERRORS)   # arr_serve.config() refuses to start on it
    with pytest.raises(SystemExit, match=key):
        m.main(["--selftest"])


@pytest.mark.parametrize("text, on", [("", False), ("KEEP_REPLACED=''\n", False), ("KEEP_REPLACED='true'\n", True),
                                      ("KEEP_REPLACED=' True '\n", True), ("KEEP_REPLACED='false'\n", False)])
def test_keep_replaced_is_off_unless_it_is_true(tmp_path, text, on):
    m = load(tmp_path, text)
    assert m.KEEP_REPLACED is on and m.CONFIG_ERRORS == []


def test_a_trailing_slash_leaves_each_pair_and_a_value_with_no_pair_takes_path_map(tmp_path):
    m = load(tmp_path, "PATH_MAP='/data/:/media/'\nSONARR_PATH_MAP='|'\nRADARR_PATH_MAP='/movies//:/media/Movies/|/:/host/'\nPLEX_PATH_MAP='/:/'\n")
    assert m.PATH_MAP == [("/data", "/media")] and m.CONFIG_ERRORS == []
    assert m.MAPS == {"radarr": [("/movies", "/media/Movies"), ("/", "/host")], "plex": [("/", "/")]}   # no sonarr map: '|' has no pair
    assert (m.mapped("/data", "sonarr"), m.mapped("/movies", "radarr"), m.mapped("/media/Movies", "radarr", True)) == ("/media", "/media/Movies", "/movies")
    assert (m.mapped("/tv/a.mkv", "radarr"), m.mapped("/media/a.mkv", "plex", True)) == ("/host/tv/a.mkv", "/media/a.mkv")


def test_map_fix_says_fix_for_a_map_that_is_set_and_set_for_one_that_is_not(tmp_path):
    m = load(tmp_path)
    assert [m.map_key(w) for w in ("sonarr", "radarr", "plex")] == [None, None, None]
    assert m.map_fix("sonarr") == "set SONARR_PATH_MAP or PATH_MAP"
    m = load(tmp_path, "PATH_MAP='/data:/media'\nPLEX_PATH_MAP='/mnt:/media'\n")
    assert [m.map_fix(w) for w in ("sonarr", "plex")] == ["fix PATH_MAP, or set SONARR_PATH_MAP", "fix PLEX_PATH_MAP"]


def test_a_keep_replaced_value_other_than_true_or_false_keeps_nothing_and_fails_the_selftest(tmp_path):
    m = load(tmp_path, "KEEP_REPLACED='yes'\n")
    assert m.KEEP_REPLACED is False and m.CONFIG_ERRORS == [
        "KEEP_REPLACED 'yes' is no switch value, so the hook keeps nothing at a grab. The values are true and false."]
    with pytest.raises(SystemExit, match="KEEP_REPLACED 'yes' is no switch value"):
        m.main(["--selftest"])
