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
"""The package as a test sees it: one object for the names the script held when it was one file. load() gives a test
file its own copy of the package, as loading the script by path did."""
import importlib
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "arr_media_guard")
SCRIPT = ("apps", "checks", "cli", "config", "convert", "logs", "plex", "process", "proof", "regrab", "remux", "report", "runner",
          "subtitles", "vault")   # the modules the script held, and report.py
ALIASES = {"arr_decide": "decide", "arr_meta": "content", "arr_status": "health", "arr_subsync": "subsync", "arr_lid": "lid",
           "arr_serve": "serve"}   # the names of the modules beside the script
sys.path.insert(0, ROOT)   # for arr_subhunt.py


class Package:
    """A read of a name gives the value in the module that defines it. A write goes to that module, and a patch reaches
    every caller, because the modules call each other through the module. A name that only imports bind, such as time,
    is written into each module that binds it. A name that two modules hold raises, because one of them imported it
    from the other, and a patch would miss its callers. The modules beside the script keep their old names, as
    hook.arr_decide."""

    def __init__(self, name):
        mods = [sys.modules[f"{name}.{m}"] for m in SCRIPT]
        held = lambda module: {k for m in mods for k, v in vars(m).items() if isinstance(v, types.ModuleType) == module}
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_mods", mods)
        object.__setattr__(self, "_imports", held(True) - held(False))   # the names only imports bind

    __file__ = os.path.join(ROOT, "arr-media-guard")   # the launcher, the path the apps run

    def _where(self, key):
        found = [m for m in self._mods if key in vars(m)]
        if key in self._imports:
            return found
        defs = [m for m in found if not isinstance(vars(m)[key], types.ModuleType)]
        if len(defs) > 1:
            raise AttributeError(f"{key} is in {', '.join(m.__name__ for m in defs)}. A module imported it from another one.")
        return defs

    def __getattr__(self, key):
        if key in ALIASES:
            return sys.modules[f"{self._name}.{ALIASES[key]}"]
        where = self._where(key)
        if not where:
            raise AttributeError(f"the package defines no {key}")
        return getattr(where[0], key)

    def __setattr__(self, key, value):
        where = self._where(key)
        if not where:
            raise AttributeError(f"the package defines no {key}")
        for m in where:
            setattr(m, key, value)


def load(name="arr_media_guard"):
    """A Package of the package loaded as name. arr_media_guard is the package itself, which arr_subhunt.py imports.
    Another name loads a new copy, with the settings of the env file at that moment and its own state."""
    if name != "arr_media_guard":
        for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            del sys.modules[k]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, os.path.join(PACKAGE, "__init__.py"), submodule_search_locations=[PACKAGE])
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    for m in SCRIPT + tuple(ALIASES.values()):
        importlib.import_module(f"{name}.{m}")
    return Package(name)


def deadlines():
    """The job's time limit, content.DEADLINE, of each copy of the package loaded so far."""
    return [m.DEADLINE for k, m in list(sys.modules.items()) if k.endswith(".content") and hasattr(m, "DEADLINE")]
