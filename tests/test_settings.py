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
"""Unit tests for the env file settings of arr-media-guard: their defaults, their values and the values the script
cannot read. Each test loads its own copy of the package with its own env file, see amg.load().

Run: pytest tests/test_settings.py
"""
import itertools
import os
import re
import subprocess
import sys

import pytest

import amg

FILES = os.path.join(os.path.dirname(__file__), "..")
POLICY = os.path.abspath(os.path.join(FILES, "examples", "policy.json"))
COUNT = itertools.count()


def load(tmp_path, text=""):
    """A copy of the package, loaded with an env file that holds text and the example policy."""
    env = tmp_path / f"env{next(COUNT)}"
    env.write_text(f"POLICY_FILE='{POLICY}'\n" + text)
    old = os.environ.get("ARR_MEDIA_GUARD_ENV")
    os.environ["ARR_MEDIA_GUARD_ENV"], os.environ["ARR_MEDIA_GUARD_LIB"] = str(env), FILES
    try:
        name = f"arr_media_guard_settings{next(COUNT)}"
        return amg.load(name)
    finally:
        os.environ["ARR_MEDIA_GUARD_ENV"] = old if old is not None else "/nonexistent/arr-media-guard.env"


def maps(m):
    """{program: pairs} of each program whose own map has a pair."""
    return {who: m.CFG.own_map(who) for who in m.MAP_KEYS if m.CFG.own_map(who)}


def test_each_app_reads_config_xml_in_its_folder(tmp_path):
    m = load(tmp_path)
    assert (m.app_dir("radarr"), m.app_dir("sonarr")) == ("/var/lib/radarr", "/var/lib/sonarr")
    folder = tmp_path / "sonarr"
    folder.mkdir()
    (folder / "config.xml").write_text("<Config><ApiKey>k3y</ApiKey></Config>")
    m = load(tmp_path, f"SONARR_DIR='{folder}'\n")
    assert m.app_dir("sonarr") == str(folder) and m.app_dir("radarr") == "/var/lib/radarr"
    assert m.api_key("sonarr") == "k3y" and not hasattr(m, "app_db")   # no feature reads an app database
    assert load(tmp_path, f"SONARR_DIR='{folder}'\nSONARR_API_KEY='fromenv'\n").api_key("sonarr") == "fromenv"


def test_the_subtitle_hunter_keys_are_secrets(tmp_path):
    """Radarr's API masks the SABnzbd and indexer keys, so the hunter reads them from the env file. A log line never
    shows them."""
    m = load(tmp_path, "SABNZBD_API_KEY='s4b-0123456789'\nNEWZNAB_API_KEY='nzb-0123456789'\nSABNZBD_URL='http://sab.lan:8080'\n"
                       "NEWZNAB_URL='http://hydra.lan:5076'\n")
    assert (m.CFG.sabnzbd_api_key, m.CFG.newznab_api_key) == ("s4b-0123456789", "nzb-0123456789")
    assert m.mask("apikey=s4b-0123456789&k=nzb-0123456789") == "apikey=<SABNZBD_API_KEY>&k=<NEWZNAB_API_KEY>"
    assert (m.CFG.sabnzbd_url, m.CFG.newznab_url) == ("http://sab.lan:8080", "http://hydra.lan:5076")
    empty = load(tmp_path).CFG
    assert (empty.sabnzbd_api_key, empty.newznab_api_key, empty.sabnzbd_url, empty.newznab_url) == ("", "", "", "")


@pytest.mark.parametrize("text, kinds", [("", {"audio", "video"}), ("REGRAB='audio, Content ,DAMAGE'\n", {"audio", "content", "damage"}),
                                         ("REGRAB='none'\n", set()), ("REGRAB=' None '\n", set()), ("REGRAB='video,video'\n", {"video"})])
def test_regrab_lists_the_kinds_that_regrab(tmp_path, text, kinds):
    m = load(tmp_path, text)
    assert m.CFG.regrab == kinds and m.CFG.errors == []


def test_an_unknown_regrab_kind_is_left_out_and_fails_the_selftest(tmp_path):
    m = load(tmp_path, "REGRAB='audio,sound,video'\n")
    assert m.CFG.regrab == {"audio", "video"}   # the safest reading: only the kinds it knows
    assert m.CFG.errors == ["REGRAB names sound, which is no re-grab kind, so it is left out. The kinds are audio, video, content, damage."]
    with pytest.raises(SystemExit) as ex:
        m.main(["--selftest"])
    assert "REGRAB names sound" in str(ex.value)


def test_name_names_the_hidden_folders(tmp_path):
    m = load(tmp_path)
    assert (m.CFG.keep_dir, m.CFG.hide_dir, m.CFG.recycle_dir) == (".arr-media-guard-originals", ".arr-media-guard-convert", ".arr-media-guard-recycle")
    assert m.CFG.errors == []
    m = load(tmp_path, "NAME='site-guard.2'\nKEEP_DIR='.kept'\nHIDE_DIR='.hidden'\n")
    assert (m.CFG.keep_dir, m.CFG.hide_dir, m.CFG.recycle_dir, m.CFG.name) == (".site-guard.2-originals", ".site-guard.2-convert", ".site-guard.2-recycle",
                                                                     "site-guard.2")
    m.main(["--selftest"])   # the old keys change nothing


@pytest.mark.parametrize("name", ["", "a/b", "has space", "../x"])
def test_a_name_that_is_no_plain_folder_name_takes_the_default_and_fails_the_selftest(tmp_path, name):
    m = load(tmp_path, f"NAME='{name}'\n")
    assert (m.CFG.keep_dir, m.CFG.hide_dir, m.CFG.recycle_dir, m.CFG.name) == (".arr-media-guard-originals", ".arr-media-guard-convert",
                                                                     ".arr-media-guard-recycle", "arr-media-guard")
    assert len(m.CFG.errors) == 1 and m.CFG.errors[0].startswith(f"NAME {name!r} holds other characters")
    with pytest.raises(SystemExit, match="NAME"):
        m.main(["--selftest"])


@pytest.mark.parametrize("text, level", [("", "fix"), ("SUBTITLES='off'\n", "off"), ("SUBTITLES='Check'\n", "check"),
                                         ("SUBTITLES='deep'\n", "deep")])
def test_subtitles_sets_the_level_of_an_import(tmp_path, text, level):
    m = load(tmp_path, text)
    assert m.CFG.subtitles == level and m.CFG.errors == []


def test_an_unknown_subtitles_level_acts_as_check_and_fails_the_selftest(tmp_path):
    m = load(tmp_path, "SUBTITLES='on'\n")
    assert m.CFG.subtitles == "check" and m.CFG.errors == [
        "SUBTITLES 'on' is no level, so the subtitle check of an import runs as check. The levels are off, check, fix, deep."]
    with pytest.raises(SystemExit, match="SUBTITLES 'on' is no level"):
        m.main(["--selftest"])


@pytest.mark.parametrize("text, kinds, error", [("REGRAB=''\n", set(), "REGRAB is blank, so nothing re-grabs. Write REGRAB='none'"),
                                                ("REGRAB=' , '\n", set(), "REGRAB is blank"),
                                                ("REGRAB='none,audio'\n", {"audio"}, "REGRAB names none, which is no re-grab kind")])
def test_a_blank_regrab_re_grabs_nothing_and_fails_the_selftest(tmp_path, text, kinds, error):
    m = load(tmp_path, text)
    assert m.CFG.regrab == kinds and len(m.CFG.errors) == 1 and m.CFG.errors[0].startswith(error), m.CFG.errors
    with pytest.raises(SystemExit, match="REGRAB"):
        m.main(["--selftest"])


@pytest.mark.parametrize("value, pairs", [("", []), ("/tv:/media/tv|/movies:/m", [("/tv", "/media/tv"), ("/movies", "/m")]),
                                          ("/tv:/media/tv|/a:/b:/c", [("/tv", "/media/tv")]), ("tv:/media/tv", []), ("/tv=/media/tv", []),
                                          ("/tv:", [])])
def test_a_path_map_pair_that_is_not_two_absolute_paths_is_left_out_and_fails_the_selftest(tmp_path, value, pairs):
    m = load(tmp_path, f"PATH_MAP='{value}'\n")
    bad = len(pairs) != len([p for p in value.split("|") if p])
    assert m.CFG.path_map == pairs and bool(m.CFG.map_error) == bad and (m.CFG.errors == [m.CFG.map_error] if bad else m.CFG.errors == [])
    if bad:
        with pytest.raises(SystemExit, match="PATH_MAP takes pairs"):
            m.main(["--selftest"])


@pytest.mark.parametrize("quote", ["'", '"', ""])
def test_each_program_map_reads_paths_with_spaces_and_an_empty_one_takes_path_map(tmp_path, quote):
    m = load(tmp_path, f"PATH_MAP={quote}/data:/media{quote}\nSONARR_PATH_MAP={quote}/mnt/TV:/media/TV|/mnt/Anime:/media/Anime{quote}\n"
                       f"RADARR_PATH_MAP={quote}{quote}\nPLEX_PATH_MAP={quote}/mnt/TV Shows:/media/TV{quote}\n")
    assert maps(m) == {"sonarr": [("/mnt/TV", "/media/TV"), ("/mnt/Anime", "/media/Anime")], "plex": [("/mnt/TV Shows", "/media/TV")]}
    assert m.CFG.path_map == [("/data", "/media")] and m.CFG.errors == [] and m.CFG.map_error is None
    assert m.mapped("/media/TV/A/a.mkv", "plex", True) == "/mnt/TV Shows/A/a.mkv" and m.mapped("/data/a.mkv", "radarr") == "/media/a.mkv"
    assert (m.map_key("sonarr"), m.map_key("radarr")) == ("SONARR_PATH_MAP", "PATH_MAP")


def image_env():
    """The env file of the image, as the Dockerfile writes it with docker/merge_env.py."""
    return subprocess.run([sys.executable, os.path.join(FILES, "docker", "merge_env.py")], capture_output=True, text=True, check=True).stdout


def env_lines(text):
    """[(key, value)] of the key lines of an env file, in their order."""
    return [(k, v) for k, sep, v in (line.partition("=") for line in text.splitlines()) if sep and not k.startswith("#")]


def test_the_image_env_file_holds_each_key_once_with_the_docker_value_in_its_place():
    """Each key of the example stays in its place, once, and a Docker value takes the place of the example's. The keys
    the example does not hold go to the end. #INSTANCE in the example is the place of INSTANCE. The Docker comments go
    with their keys, and the note at the top of the Docker file stays out."""
    text = image_env()
    example, docker = (env_lines(open(os.path.join(FILES, *f)).read()) for f in (("examples", "arr-media-guard.env"), ("docker", "arr-media-guard.env")))
    keys, at = [k for k, _ in env_lines(text)], [k for k, _ in example].index("NAME") + 1
    assert keys == [k for k, _ in example][:at] + ["INSTANCE"] + [k for k, _ in example][at:] + ["WEBHOOK_USER", "WEBHOOK_PASSWORD", "AUDIT_TIME"]
    assert dict(env_lines(text)) == dict(example + docker)   # the Docker value wins, and every other value is the example's
    assert "# Mount a named volume here. The state store is SQLite in WAL mode and needs a local disk.\nSTATE_DIR='/config/state'\n" in text
    assert "by their compose service names.\nRADARR_URL='http://radarr:7878'\nSONARR_URL='http://sonarr:8989'\n# Each app's own folder" in text
    assert "\nNEWZNAB_URL=''\n\n# --- Docker ---\n# The user and password of the Webhook connection." in text
    assert "merge_env.py" not in text and text.endswith("\nAUDIT_TIME='07:30'\n")


def test_the_docker_env_file_keeps_every_map_on_path_map_until_one_is_set(tmp_path):
    """The image writes its env file, see image_env(). A line added after it wins."""
    text = image_env()
    m = load(tmp_path, text)
    assert (maps(m), m.CFG.path_map, m.CFG.errors, m.CFG.keep_replaced) == ({}, [], [], False)
    m = load(tmp_path, text + "PATH_MAP='/tv:/media/TV'\nRADARR_PATH_MAP='/movies:/media/Movies'\nPLEX_PATH_MAP='/mnt/Movies:/media/Movies'\n")
    assert maps(m) == {"radarr": [("/movies", "/media/Movies")], "plex": [("/mnt/Movies", "/media/Movies")]} and m.CFG.errors == []


@pytest.mark.parametrize("key", ["SONARR_PATH_MAP", "RADARR_PATH_MAP", "PLEX_PATH_MAP"])
def test_a_bad_pair_in_a_program_map_fails_the_selftest_and_stops_the_listener(tmp_path, key):
    m = load(tmp_path, f"PATH_MAP='/tv=/media/tv'\n{key}='/a:/b|/c:d'\n")
    assert maps(m)[key.split("_")[0].lower()] == [("/a", "/b")]
    assert m.CFG.errors == ["PATH_MAP takes pairs APP_PATH:LOCAL_PATH of absolute paths, joined by '|'. A pair that is not one is left out.",
                               f"{key} takes pairs {'PLEX' if key == 'PLEX_PATH_MAP' else 'APP'}_PATH:LOCAL_PATH of absolute paths, joined by '|'. "
                               "A pair that is not one is left out."]
    assert m.CFG.map_error == " ".join(m.CFG.errors)   # arr_serve.config() refuses to start on it
    with pytest.raises(SystemExit, match=key):
        m.main(["--selftest"])


@pytest.mark.parametrize("text, on", [("", False), ("KEEP_REPLACED=''\n", False), ("KEEP_REPLACED='true'\n", True),
                                      ("KEEP_REPLACED=' True '\n", True), ("KEEP_REPLACED='false'\n", False)])
def test_keep_replaced_is_off_unless_it_is_true(tmp_path, text, on):
    m = load(tmp_path, text)
    assert m.CFG.keep_replaced is on and m.CFG.errors == []


def test_a_trailing_slash_leaves_each_pair_and_a_value_with_no_pair_takes_path_map(tmp_path):
    m = load(tmp_path, "PATH_MAP='/data/:/media/'\nSONARR_PATH_MAP='|'\nRADARR_PATH_MAP='/movies//:/media/Movies/|/:/host/'\nPLEX_PATH_MAP='/:/'\n")
    assert m.CFG.path_map == [("/data", "/media")] and m.CFG.errors == []
    assert maps(m) == {"radarr": [("/movies", "/media/Movies"), ("/", "/host")], "plex": [("/", "/")]}   # no sonarr map: '|' has no pair
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
    assert m.CFG.keep_replaced is False and m.CFG.errors == [
        "KEEP_REPLACED 'yes' is no switch value, so the hook keeps nothing at a grab. The values are true and false."]
    with pytest.raises(SystemExit, match="KEEP_REPLACED 'yes' is no switch value"):
        m.main(["--selftest"])


@pytest.mark.parametrize("text, error", [
    ("REGRAB_CAP='ten'\n", "REGRAB_CAP 'ten' is no whole number, so the cap is 0 and every fault only alerts."),
    ("REPACK_MAX_GB='30 GB'\n", "REPACK_MAX_GB '30 GB' is no number, so the cap is 0 GB and every remux is skipped."),
    ("REPACK_MAX_GB='nan'\n", "REPACK_MAX_GB 'nan' is no number, so the cap is 0 GB and every remux is skipped.")])
def test_a_cap_that_is_no_number_is_0_and_fails_the_selftest(tmp_path, text, error):
    """The script still loads, so the hook takes the import and the worker logs the error. A cap of 0 re-grabs and
    remuxes nothing."""
    m = load(tmp_path, text)
    assert m.CFG.errors == [error] and (m.CFG.regrab_cap, m.CFG.repack_max) == ((0, 30e9) if "REGRAB_CAP" in text else (30, 0))
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert m.repack_skip(str(f), os.stat(f)) == (None if "REGRAB_CAP" in text else "over the 0 GB repack cap")
    with pytest.raises(SystemExit, match=error.split()[0]):
        m.main(["--selftest"])
    m = load(tmp_path, "REGRAB_CAP='5'\nREPACK_MAX_GB='2.5'\n")
    assert (m.CFG.regrab_cap, m.CFG.repack_max, m.CFG.errors) == (5, 2.5e9, [])


SWITCH = "is no switch value, so {}. The values are true and false."
WHOLE = "is not a whole number of {} or more, so it counts as {}."


@pytest.mark.parametrize("line, field, value, error", [
    ("HEADER_REPAIR='off'", "header_repair", True, "HEADER_REPAIR 'off' " + SWITCH.format("the hook repairs a broken header")),
    ("RESTORE='no'", "restore", True, "RESTORE 'no' " + SWITCH.format("a re-grab of a broken upgrade puts the old file back")),
    ("CONVERT='yes'", "convert", False, "CONVERT 'yes' " + SWITCH.format("the hook converts no import")),
    ("HOOK_WORKERS='0'", "hook_workers", 1, "HOOK_WORKERS '0' is not a whole number of 1 or more, so 1 runs"),
    ("KEEP_ORIGINALS_DAYS='a week'", "keep_days", 7, "KEEP_ORIGINALS_DAYS 'a week' " + WHOLE.format(0, 7)),
    ("KEEP_ORIGINALS_DAYS='-3'", "keep_days", 0, "KEEP_ORIGINALS_DAYS '-3' " + WHOLE.format(0, 0)),
    ("CONVERT_MAX_FILES='many'", "convert_max", 200, "CONVERT_MAX_FILES 'many' " + WHOLE.format(1, 200)),
    ("CONVERT_MAX_FILES='0'", "convert_max", 1, "CONVERT_MAX_FILES '0' " + WHOLE.format(1, 1)),
    ("SCAN_WORKERS='2.5'", "scan_workers", 1, "SCAN_WORKERS '2.5' " + WHOLE.format(1, 1)),
    ("CONVERT_WORKERS='-1'", "convert_workers", 1, "CONVERT_WORKERS '-1' " + WHOLE.format(1, 1)),
    ("REPACK_MAX_GB='lots'", "repack_max", 0, "REPACK_MAX_GB 'lots' is no number, so the cap is 0 GB and every remux is skipped."),
    ("SUBTITLES=''", "subtitles", "check", "SUBTITLES '' is no level, so the subtitle check of an import runs as check. The levels are "
                                           "off, check, fix, deep."),
    ("REGRAB='sound'", "regrab", set(), "REGRAB names sound, which is no re-grab kind, so it is left out. The kinds are audio, video, "
                                        "content, damage."),
    ("PLEX_PATH_MAP='/a'", "plex_path_map", [], "PLEX_PATH_MAP takes pairs PLEX_PATH:LOCAL_PATH of absolute paths, joined by '|'. A pair "
                                                "that is not one is left out.")])
def test_each_parser_reads_a_bad_value_the_safest_way_and_the_selftest_names_it(tmp_path, line, field, value, error):
    """One case or more per parser: switch, whole number, number, level, list and path map. The script loads, the field
    holds the documented reading, and the error names the key."""
    m = load(tmp_path, line + "\n")
    assert getattr(m.CFG, field) == value and m.CFG.errors == [error]
    with pytest.raises(SystemExit, match="selftest failed: " + re.escape(error)):
        m.main(["--selftest"])


FIELDS = ("keep_days", "hook_workers", "convert_max", "scan_workers", "convert_workers", "regrab_cap", "repack_max", "header_repair",
          "restore", "convert", "keep_replaced", "audit_time", "policy_file")


def test_a_missing_key_takes_its_default_and_an_empty_one_its_empty_reading(tmp_path):
    """An empty KEEP_ORIGINALS_DAYS keeps nothing, an empty AUDIT_TIME runs no nightly audit, and every other empty key
    in FIELDS takes its default."""
    defaults = (7, 1, 200, 1, 1, 30, 30e9, True, True, False, False, "07:30", POLICY)
    m = load(tmp_path)
    assert tuple(getattr(m.CFG, f) for f in FIELDS) == defaults and m.CFG.errors == []
    m = load(tmp_path, "".join(f"{k}=''\n" for k in ("KEEP_ORIGINALS_DAYS", "HOOK_WORKERS", "CONVERT_MAX_FILES", "SCAN_WORKERS", "CONVERT_WORKERS",
                                                     "REGRAB_CAP", "REPACK_MAX_GB", "HEADER_REPAIR", "RESTORE", "CONVERT", "KEEP_REPLACED",
                                                     "AUDIT_TIME")))
    assert tuple(getattr(m.CFG, f) for f in FIELDS) == (0,) + defaults[1:11] + ("", POLICY) and m.CFG.errors == []


def test_a_value_reads_with_spaces_and_in_any_case(tmp_path):
    m = load(tmp_path, "HEADER_REPAIR=' False '\nCONVERT='TRUE'\nRESTORE='false'\nHOOK_WORKERS=' 4 '\nREPACK_MAX_GB='2.5'\n")
    assert (m.CFG.header_repair, m.CFG.convert, m.CFG.restore, m.CFG.hook_workers, m.CFG.repack_max) == (False, True, False, 4, 2.5e9)
    assert m.CFG.errors == []


def test_each_app_takes_its_own_keys_and_mask_hides_each_secret(tmp_path):
    m = load(tmp_path, "RADARR_URL='http://r.invalid:1'\nRADARR_API_KEY='rkey-0123456789'\nSONARR_DIR=''\nSONARR_PATH_MAP='/tv:/media/tv'\n"
                       "PLEX_TOKEN='ptok-0123456789'\nDISCORD_WEBHOOK='https://d.invalid/hook'\nTMDB_TOKEN='tmdb-0123456789'\n")
    r, s = m.CFG.apps["radarr"], m.CFG.apps["sonarr"]
    assert (r.url, r.api_key, r.dir, r.path_map) == ("http://r.invalid:1", "rkey-0123456789", "/var/lib/radarr", [])
    assert (s.url, s.api_key, s.dir, s.path_map) == ("http://127.0.0.1:8989", "", "/var/lib/sonarr", [("/tv", "/media/tv")])
    assert m.mask("rkey-0123456789 ptok-0123456789 https://d.invalid/hook tmdb-0123456789") == \
        "<RADARR_API_KEY> <PLEX_TOKEN> <DISCORD_WEBHOOK> <TMDB_TOKEN>"


def test_mask_leaves_a_short_secret_alone_and_masks_a_long_webhook_password(tmp_path):
    """A secret under 12 characters may be a word of a path, as WEBHOOK_PASSWORD='movies' was. The undo command of an
    editing line must keep its path. A long WEBHOOK_PASSWORD is masked, as the 32 characters the listener generates are."""
    m = load(tmp_path, "WEBHOOK_PASSWORD='movies'\nRADARR_API_KEY='media'\nPLEX_TOKEN='0123456789ab'\n")
    assert m.mask("mkvpropedit /media/movies/A.mkv ?X-Plex-Token=0123456789ab") == "mkvpropedit /media/movies/A.mkv ?X-Plex-Token=<PLEX_TOKEN>"
    assert load(tmp_path, "WEBHOOK_PASSWORD='a-long-password-1234'\n").mask("a-long-password-1234") == "<WEBHOOK_PASSWORD>"


def test_the_errors_keep_the_order_the_worker_logged_them_in(tmp_path):
    """HOOK_WORKERS comes first, as the worker logged it before the other errors. KEEP_REPLACED comes after the maps."""
    m = load(tmp_path, "KEEP_REPLACED='maybe'\nPATH_MAP='x'\nSUBTITLES='x'\nREPACK_MAX_GB='x'\nREGRAB=''\nNAME='a b'\nREGRAB_CAP='x'\n"
                       "HOOK_WORKERS='x'\nCONVERT='x'\n")
    assert [e.split()[0] for e in m.CFG.errors] == ["HOOK_WORKERS", "REGRAB_CAP", "NAME", "REGRAB", "REPACK_MAX_GB", "SUBTITLES", "PATH_MAP",
                                                    "KEEP_REPLACED", "CONVERT"]


def test_instances_add_named_apps_that_read_their_own_keys(tmp_path):
    """APP_INSTANCES names more instances as name:program pairs. Each reads <KEY>_URL, <KEY>_API_KEY, <KEY>_DIR and
    <KEY>_PATH_MAP, with KEY the name in upper case and '-' as '_'. The default instances keep their keys."""
    m = load(tmp_path, "APP_INSTANCES=' sonarr-4k:sonarr , Radarr-UHD:Radarr'\nSONARR_4K_URL='http://s4k.invalid:8990'\nSONARR_4K_API_KEY='k4k-0123456789'\n"
                       "SONARR_4K_PATH_MAP='/tv:/media/tv4k'\nRADARR_UHD_URL='http://r.invalid:7879'\nRADARR_UHD_DIR='/srv/radarr-uhd'\n"
                       "PATH_MAP='/data:/media'\nSONARR_PATH_MAP='/tv:/media/tv'\n")
    assert m.CFG.errors == [] and list(m.CFG.apps) == ["radarr", "sonarr", "sonarr-4k", "Radarr-UHD"]
    s, r = m.CFG.apps["sonarr-4k"], m.CFG.apps["Radarr-UHD"]
    assert (s.url, s.api_key, s.dir, s.path_map, s.program) == ("http://s4k.invalid:8990", "k4k-0123456789", "/var/lib/sonarr-4k", [("/tv", "/media/tv4k")], "sonarr")
    assert (r.url, r.api_key, r.dir, r.path_map, r.program) == ("http://r.invalid:7879", "", "/srv/radarr-uhd", [], "radarr")
    assert m.CFG.apps["sonarr"].path_map == [("/tv", "/media/tv")] and m.CFG.apps["sonarr"].program == "sonarr"
    assert (m.mapped("/tv/a.mkv", "sonarr-4k"), m.mapped("/tv/a.mkv", "sonarr"), m.mapped("/data/a.mkv", "Radarr-UHD")) == \
        ("/media/tv4k/a.mkv", "/media/tv/a.mkv", "/media/a.mkv")   # an instance with no map of its own takes PATH_MAP
    assert (m.map_key("sonarr-4k"), m.map_key("Radarr-UHD"), m.map_fix("Radarr-UHD")) == \
        ("SONARR_4K_PATH_MAP", "PATH_MAP", "fix PATH_MAP, or set RADARR_UHD_PATH_MAP")
    assert m.mask("key k4k-0123456789") == "key <SONARR_4K_API_KEY>"
    assert {a: (type(x).__name__, x.app, x.name) for a, x in m.ARR.items()} == {
        "radarr": ("Radarr", "radarr", "Radarr"), "sonarr": ("Sonarr", "sonarr", "Sonarr"), "sonarr-4k": ("Sonarr", "sonarr-4k", "Sonarr-4k"),
        "Radarr-UHD": ("Radarr", "Radarr-UHD", "Radarr-uhd")}
    assert (m.program("sonarr-4k"), m.program("Radarr-UHD"), m.program(None)) == ("sonarr", "radarr", None)


@pytest.mark.parametrize("text, why", [
    ("APP_INSTANCES='sonarr-4k'\n", "is no name:program pair"),
    ("APP_INSTANCES='sonarr 4k:sonarr'\nSONARR 4K_URL='http://x.invalid'\n", "has other characters than letters, digits and '-' in its name"),
    ("APP_INSTANCES='-4k:sonarr'\n_4K_URL='http://x.invalid'\n", "has other characters than letters, digits and '-' in its name"),
    ("APP_INSTANCES='a--b:sonarr'\nA__B_URL='http://x.invalid'\n", "has other characters than letters, digits and '-' in its name"),
    ("APP_INSTANCES='lidarr-1:lidarr'\nLIDARR_1_URL='http://x.invalid'\n", "names no program. The programs are radarr and sonarr"),
    ("APP_INSTANCES='Sonarr:radarr'\n", "has the name of another instance"),
    ("APP_INSTANCES='tv-1:sonarr,TV-1:radarr'\nTV_1_URL='http://x.invalid'\n", "has the name of another instance"),
    ("APP_INSTANCES='plex:sonarr'\nPLEX_URL='http://x.invalid'\n", "has a name whose keys PLEX_URL and the like belong to other settings"),
    ("APP_INSTANCES='state:radarr'\nSTATE_URL='http://x.invalid'\n", "has a name whose keys STATE_URL and the like belong to other settings"),
    ("APP_INSTANCES='sonarr-4k:sonarr'\nSONARR_4K_URL=''\n", "has no SONARR_4K_URL")])
def test_a_bad_instances_entry_is_left_out_and_fails_the_selftest(tmp_path, text, why):
    """A bad entry defines no instance, so no event reaches it, and --selftest names it. The good entries stay."""
    m = load(tmp_path, text)
    entry = text.split("'")[1].split(",")[-1]
    assert m.CFG.errors == [f"The APP_INSTANCES entry {entry!r} {why}, so it is left out."]
    assert list(m.CFG.apps) == ["radarr", "sonarr"] + (["tv-1"] if "tv-1" in text else []) and list(m.ARR) == list(m.CFG.apps)
    with pytest.raises(SystemExit, match="selftest failed: The APP_INSTANCES entry"):
        m.main(["--selftest"])


def test_a_bad_pair_in_an_instance_map_fails_the_selftest_and_stops_the_listener(tmp_path):
    m = load(tmp_path, "APP_INSTANCES='sonarr-4k:sonarr'\nSONARR_4K_URL='http://x.invalid'\nSONARR_4K_PATH_MAP='/tv:/a|tv:/b'\n")
    assert m.CFG.apps["sonarr-4k"].path_map == [("/tv", "/a")] and m.CFG.map_error == m.CFG.errors[0] == (
        "SONARR_4K_PATH_MAP takes pairs APP_PATH:LOCAL_PATH of absolute paths, joined by '|'. A pair that is not one is left out.")


def test_no_instance_name_takes_the_keys_of_another_setting():
    """TAKEN holds the start of each other key that ends in _URL, _API_KEY, _DIR or _PATH_MAP, so an instance named after
    it never reads that setting as its own."""
    import arr_media_guard.config as config
    source = open(config.__file__).read()
    starts = {k.rsplit(s, 1)[0] for k in re.findall(r'"([A-Z][A-Z0-9_]*)"', source) for s in ("_URL", "_API_KEY", "_DIR", "_PATH_MAP")
              if k.endswith(s)}
    assert starts and starts <= set(config.TAKEN) | {"SONARR", "RADARR"}, starts
