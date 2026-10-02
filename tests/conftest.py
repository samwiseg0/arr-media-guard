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

import pytest

import amg


@pytest.fixture(autouse=True)
def no_time_limit():
    """content.DEADLINE, the job's time limit, is module state. A probe that a test calls on its own starts it, so each
    test starts and ends with none, in each copy of the package."""
    for d in amg.deadlines():
        d.stop()
    yield
    for d in amg.deadlines():
        d.stop()


@pytest.fixture
def settings(request, monkeypatch):
    """settings(**changes) swaps a changed copy of the package's CFG in for one test. A field name takes its new value.
    An app name takes a dict of that app's fields. The test file holds the package as hook or h, see amg.load()."""
    script = getattr(request.module, "hook", None) or request.module.h

    def change(**changes):
        apps = {app: dataclasses.replace(a, **changes.pop(app, {})) for app, a in script.CFG.apps.items()}
        monkeypatch.setattr(script, "CFG", dataclasses.replace(script.CFG, apps=apps, **changes))
    return change
