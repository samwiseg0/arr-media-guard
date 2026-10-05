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
"""The settings fixture, shared by the test files that load the package."""
import dataclasses
import io
import os
import socket
import urllib.request
import urllib.response

import pytest

os.environ["AMG_INVARIANTS"] = "1"   # every test checks the safety rules of a block move, see subsync.INVARIANTS

import amg  # noqa: E402


@pytest.fixture(autouse=True)
def no_time_limit():
    """content.DEADLINE, the job's time limit, is module state. A probe that a test calls on its own starts it, so each
    test starts and ends with none, in each copy of the package."""
    for d in amg.deadlines():
        d.stop()
    yield
    for d in amg.deadlines():
        d.stop()


LOCAL = ("127.0.0.1", "localhost", "::1")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches a host other than this one, so no test reaches a real service. A connection to another host
    fails, as one to a host with no server does, and urllib reads the request as it would for a real host first.
    TMDB's key check of the start check answers that the key is valid, so a start check or a selftest prints only the
    services its test sets up. A test that fakes urlopen replaces that answer."""
    connect, urlopen = socket.create_connection, urllib.request.urlopen

    def guard(address, *args, **kwargs):
        if address[0] not in LOCAL:
            raise OSError(f"the tests reach no network, and this call asked {address[0]}")
        return connect(address, *args, **kwargs)

    def tmdb_ok(req, *args, **kwargs):
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        if url == "https://api.themoviedb.org/3/authentication":
            return urllib.response.addinfourl(io.BytesIO(b'{"success": true}'), {}, url, 200)
        return urlopen(req, *args, **kwargs)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(urllib.request, "urlopen", tmdb_ok)


@pytest.fixture
def settings(request, monkeypatch):
    """settings(**changes) swaps a changed copy of the package's CFG in for one test. A field name takes its new value.
    An app name takes a dict of that app's fields. The test file holds the package as hook or h, see amg.load()."""
    script = getattr(request.module, "hook", None) or request.module.h

    def change(**changes):
        apps = {app: dataclasses.replace(a, **changes.pop(app, {})) for app, a in script.CFG.apps.items()}
        monkeypatch.setattr(script, "CFG", dataclasses.replace(script.CFG, apps=apps, **changes))
    return change
