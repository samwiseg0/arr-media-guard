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
"""Unit tests for arr_status.py, the status file a monitoring agent reads.

Run: pytest tests/test_arr_status.py
"""
import json
import multiprocessing
import os
import re
import stat
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
import arr_status as S  # noqa: E402

T0 = 1_790_000_000   # a fixed epoch, 800 seconds into its hour


def read(d):
    with open(os.path.join(d, S.FILE)) as f:
        return json.load(f)


def test_first_write_has_every_check_and_the_run_time(tmp_path):
    assert S.record(str(tmp_path), "tmdb", "ok", now=T0) == (True, "")
    data = read(tmp_path)
    assert data["version"] == 1 and data["last_hook_run"] == T0
    assert data["checks"]["tmdb"]["status"] == "ok" and data["checks"]["tmdb"]["since"] == T0
    assert data["checks"]["policy"]["status"] == "unknown"   # never recorded, still present for JSONPath
    assert data["checks"]["policy"]["last_24h"] == {"ok": 0, "failed": 0}


def test_a_record_without_touch_leaves_the_run_time(tmp_path):
    """--selftest records the policy on dry runs too. That must never hide a stopped audit timer."""
    S.record(str(tmp_path), "policy", "ok", now=T0)
    S.record(str(tmp_path), "policy", "failed", "bad", now=T0 + 600, touch=False)
    data = read(tmp_path)
    assert data["last_hook_run"] == T0 and data["checks"]["policy"]["status"] == "failed"


def test_the_agent_can_read_the_file(tmp_path):
    S.record(str(tmp_path), "policy", "ok", now=T0)
    assert stat.S_IMODE(os.stat(tmp_path / S.FILE).st_mode) == 0o644


def test_since_moves_only_on_a_change_and_checked_on_every_write(tmp_path):
    d = str(tmp_path)
    S.record(d, "tmdb", "tmdb_token_rejected", "HTTPError: HTTP Error 401: Unauthorized", now=T0)
    S.record(d, "tmdb", "tmdb_token_rejected", "HTTPError: HTTP Error 401: Unauthorized", now=T0 + 600)
    c = read(d)["checks"]["tmdb"]
    assert (c["status"], c["since"], c["checked"]) == ("token_rejected", T0, T0 + 600)
    S.record(d, "tmdb", "ok", now=T0 + 900)
    c = read(d)["checks"]["tmdb"]
    assert (c["status"], c["since"], c["checked"]) == ("ok", T0 + 900, T0 + 900)
    assert c["error"] == "HTTPError: HTTP Error 401: Unauthorized" and c["error_time"] == T0 + 600   # kept after recovery


def test_arr_meta_codes_map_to_statuses(tmp_path):
    d = str(tmp_path)
    for code, want in [("found", "ok"), ("no_record", "ok"), ("ok", "ok"), ("tmdb_unavailable", "unavailable"),
                       ("tmdb_token_missing", "token_missing"), ("tmdb_token_rejected", "token_rejected")]:
        S.record(d, "tmdb", code, now=T0)
        assert read(d)["checks"]["tmdb"]["status"] == want


def test_every_arr_meta_code_maps_to_a_status():
    """Every code arr_meta.py sets through _down() or KEY_BROKEN, read from its source, is a known status."""
    src = os.path.join(ROOT, "arr_meta.py")
    if not os.path.exists(src):
        pytest.skip("arr_meta.py is not in this tree")
    with open(src) as f:
        lines = [line for line in f if "_down(" in line or line.startswith("KEY_BROKEN")]
    codes = set(re.findall(r'"(tmdb_[a-z_]+)"', "".join(lines))) | {"found", "no_record"}
    assert {"tmdb_unavailable", "tmdb_token_missing", "tmdb_token_rejected"} <= codes
    for code in codes:
        assert S.ALIASES.get(code, code) in S.STATUSES["tmdb"], code


@pytest.mark.parametrize("key,code,want", [("tmdb", "tmdb_rate_limited", "unavailable"), ("policy", "no_policy", "failed")])
def test_an_unknown_code_records_a_failure_and_keeps_the_code(tmp_path, key, code, want):
    written, note = S.record(str(tmp_path), key, code, "why", now=T0)
    assert written and code in note
    c = read(tmp_path)["checks"][key]
    assert c["status"] == want and c["error"] == f"{code}: why"


def test_an_unknown_check_writes_nothing(tmp_path):
    written, note = S.record(str(tmp_path), "plex", "ok", now=T0)
    assert not written and "plex" in note and not os.listdir(tmp_path)


class OutOfTime(Exception):
    """Stands in for arr_meta.OutOfTime, which record() knows by name only."""


@pytest.mark.parametrize("exc", [TimeoutError, OutOfTime])
def test_the_time_limit_is_never_swallowed(tmp_path, monkeypatch, exc):
    def late(*a, **k):
        raise exc("time is up")
    monkeypatch.setattr(S.json, "dump", late)
    with pytest.raises(exc):
        S.record(str(tmp_path), "tmdb", "ok", now=T0)
    monkeypatch.setattr(S.json, "load", late)
    (tmp_path / S.FILE).write_text("{}")
    with pytest.raises(exc):
        S.record(str(tmp_path), "tmdb", "ok", now=T0)


def test_error_text_is_cut_and_has_no_token(tmp_path):
    jwt = "eyJhbGciOiJIUzI1NiJ9." + "a" * 120 + "." + "b" * 43
    S.record(str(tmp_path), "tmdb", "token_rejected", f"Bearer {jwt} refused " + "x " * 200, now=T0)
    err = read(tmp_path)["checks"]["tmdb"]["error"]
    assert "a" * 32 not in err and "b" * 32 not in err and "<redacted>" in err
    assert len(err) == 200
    S.record(str(tmp_path), "policy", "failed", "JSONDecodeError: /etc/arr-media-guard.policy.json line 1", now=T0)
    assert read(tmp_path)["checks"]["policy"]["error"] == "JSONDecodeError: /etc/arr-media-guard.policy.json line 1"


def test_ok_keeps_no_new_error(tmp_path):
    S.record(str(tmp_path), "policy", "ok", "ignored", now=T0)
    assert read(tmp_path)["checks"]["policy"]["error"] == ""


def test_counters_cover_the_last_24_hours(tmp_path):
    d = str(tmp_path)
    S.record(d, "tmdb", "unavailable", "timeout", now=T0)
    S.record(d, "tmdb", "unavailable", "timeout", now=T0 + 60)
    S.record(d, "tmdb", "ok", now=T0 + 3 * 3600)
    assert read(d)["checks"]["tmdb"]["last_24h"] == {"ok": 1, "unavailable": 2, "token_missing": 0, "token_rejected": 0}
    S.record(d, "tmdb", "ok", now=T0 + S.DAY + 60)   # the first hour has aged out
    c = read(d)["checks"]["tmdb"]
    assert c["last_24h"] == {"ok": 2, "unavailable": 0, "token_missing": 0, "token_rejected": 0}
    assert len(c["hours"]) == 2


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    d = str(tmp_path)
    S.record(d, "policy", "ok", now=T0)
    before = (tmp_path / S.FILE).read_text()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(S.json, "dump", boom)
    written, note = S.record(d, "policy", "failed", "x", now=T0 + 1)
    assert not written and note == "arr_status: OSError: disk full"
    assert (tmp_path / S.FILE).read_text() == before
    assert sorted(os.listdir(d)) == [S.FILE, S.FILE + ".lock"]


@pytest.mark.parametrize("text", [
    '{"version": 1, "checks": {"tmdb": {"status": "tok',
    '{"version": 1, "checks": {"tmdb": "x"}}',
    '{"version": 1, "checks": {"tmdb": {"hours": {"abc": {}}}}}',
    '{"version": 1, "checks": {"tmdb": {"hours": []}}}',
    '{"version": 1, "checks": []}',
    '[1, 2]',
    '{"version": 2, "checks": {}}',
])
def test_a_broken_or_wrong_shape_file_starts_fresh(tmp_path, text):
    (tmp_path / S.FILE).write_text(text)
    assert S.record(str(tmp_path), "policy", "failed", "x", now=T0) == (True, "")
    assert read(tmp_path)["checks"]["tmdb"]["status"] == "unknown"
    assert S.record(str(tmp_path), "tmdb", "ok", now=T0) == (True, "")   # and the next write works too


def test_an_unwritable_dir_returns_false(tmp_path):
    written, note = S.record(str(tmp_path / "missing"), "tmdb", "ok", now=T0)
    assert not written and note.startswith("arr_status: FileNotFoundError")


def _hammer(d, key, n):
    for _ in range(n):
        S.record(d, key, "ok", now=T0)


def test_concurrent_writers_lose_nothing(tmp_path):
    d = str(tmp_path)
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_hammer, args=(d, key, 25)) for key in ("tmdb", "policy", "tmdb", "policy")]
    for p in procs: p.start()
    for p in procs: p.join()
    checks = read(d)["checks"]
    assert checks["tmdb"]["last_24h"]["ok"] == 50 and checks["policy"]["last_24h"]["ok"] == 50
