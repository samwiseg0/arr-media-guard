# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""arr-media-guard. The launcher arr-media-guard runs cli.main(), and the launcher arr-media-guard-subhunt runs the
subtitle hunter in arr_subhunt.py beside it. docs/development.md, "Code layout", names what each module holds."""

import os

# onnxruntime 1.30 telemetry writes a :memory:.ses file into the working folder of a helper when HOME is not writable,
# and an empty mat-debug log into the temp folder. Every helper process inherits this.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
from . import ff  # noqa: E402,F401  ff.py installs the launch guard, so each process that imports the package has it
