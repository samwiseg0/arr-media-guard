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
"""Unit tests for the two launchers and the package layout. Each test runs real programs in fresh interpreters.

Run: pytest tests/test_launcher.py
"""
import os
import subprocess
import sys

import amg


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
