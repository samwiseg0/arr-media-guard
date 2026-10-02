#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The env file of the image, from examples/arr-media-guard.env and docker/arr-media-guard.env. The Dockerfile runs it
once and keeps the output as docker/arr-media-guard.env.example. The start script writes that file into /config.

  python3 docker/merge_env.py > docker/arr-media-guard.env.example
"""
import os, sys


def merge(example, docker):
    """The lines of example, where each key of docker takes the place of the same key, with the comment lines right
    above it in docker. A commented key in example, as #INSTANCE, is a place too. The keys that example does not hold
    go to the end, in the order of docker. A comment that a blank line parts from its key is left out."""
    blocks, run = {}, []   # key: the comment lines right above it in docker, then its line
    for line in docker.splitlines():
        if line.startswith("#"):
            run.append(line)
            continue
        key, sep, _ = line.partition("=")
        if sep:
            blocks[key] = run + [line]
        run = []
    out = []
    for line in example.splitlines():
        out += blocks.pop(line.partition("=")[0].lstrip("#"), [line])
    if blocks:
        out += [""] + [x for block in blocks.values() for x in block]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "examples", "arr-media-guard.env")) as a, open(os.path.join(here, "arr-media-guard.env")) as b:
        sys.stdout.write(merge(a.read(), b.read()))
