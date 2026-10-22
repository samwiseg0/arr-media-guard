# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""arr-media-guard. The launcher arr-media-guard runs cli.main(), and the launcher arr-media-guard-subhunt runs the
subtitle hunter in arr_subhunt.py beside it. docs/development.md, "Code layout", names what each module holds."""

from . import ff  # noqa: E402,F401  ff.py installs the launch guard, so each process that imports the package has it
