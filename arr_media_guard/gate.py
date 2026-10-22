# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The write gate of a queued plan (docs/design.md, "Queued plans"). A job that applies the plan row of a dry run
makes each write through allow(). allow() compares the write with the row, and runs it only when they agree. A guard
in the job's process, an audit hook, fails each write on the job's file or its sidecars that no allow() let through,
see armed()."""
import contextlib, os, shlex, sys

from . import cli


class PlanRefused(Exception):
    """A write of a queued plan differs from the plan row of its dry run, see compare(). Its argument is the plan_diff().
    process.process() then ends the run with the outcome plan_refused, see process.refuse()."""


class Escaped(AssertionError):
    """A write on the file of a queued job or on one of its sidecars that no allow() let through, see audit()."""


# The guard of the job that runs: the folder and the stem of its file, the kinds of the writes allow() runs now, the
# kinds whose allow() waits for its own compare, the first write the guard failed, and whether audit() is installed.
STATE = {"folder": None, "stem": None, "open": [], "late": [], "escaped": None, "hooked": False}


def differs(ctx, mine, against=None):
    """plan_diff() of mine, what a write writes, against the same fields of ctx.row, the plan row of the dry run, or of
    against, the part of the row that a write after a remux can still make, see process.replanned(). A run with no row
    compares nothing: an import, --check, a dry pass, and the rest of a job after its own conversion, see
    process.conversion(). An empty field and a missing one are the same. A field that a deep analysis or a recheck
    leaves empty never differs, as it writes nothing there."""
    if ctx.row is None or not ctx.apply:
        return {}
    row = ctx.row if against is None else against
    return {k: v for k, v in cli.plan_diff({k: row.get(k) for k in mine}, mine).items() if any(v) and (v[1] or not ctx.background)}


def compare(ctx, against=None, **mine):
    """Raise PlanRefused when mine differs from the row, see differs()."""
    if diff := differs(ctx, mine, against):
        raise PlanRefused(diff)


def let(kind):
    """Let the writes of kind through while a job is armed, until its allow() ends, see audit()."""
    if STATE["folder"] is not None:
        STATE["open"].append(kind)


def opened(ctx, kind, **mine):
    """The late compare of the allow() of kind, see allow(): compare mine with the row, see compare(), then let the
    writes of kind through. Outside that allow() it lets nothing through, and neither does a dry pass."""
    compare(ctx, **mine)
    if kind in STATE["late"]:
        STATE["late"].remove(kind)
        if ctx.apply:
            let(kind)


def allow(ctx, kind, run, late=False, against=None, **mine):
    """Run run(), a write of kind, and return what it returns. The fields of mine are what it writes. A field that
    differs from the row, or from against, raises PlanRefused before run() starts, see compare(). late leaves the
    compare to run() itself, through opened(), as a conversion compares its digest after its subtitle check, see
    convert.convert(). Until then the guard lets none of its writes through. A dry pass writes nothing, so its allow()
    lets no write through, and the guard fails each write of that pass."""
    n, m = len(STATE["open"]), len(STATE["late"])
    try:
        if late and STATE["folder"] is not None:
            STATE["late"].append(kind)
        elif not late:
            compare(ctx, against, **mine)
            if ctx.apply:
                let(kind)
        return run()
    finally:
        del STATE["open"][n:], STATE["late"][m:]


WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
MOVES = {"os.rename": (0, 1), "os.remove": (0,), "os.link": (1,), "os.symlink": (1,), "os.truncate": (0,), "shutil.move": (0, 1),
         "shutil.copyfile": (1,)}   # the audit events of a file write, with the places of the paths it writes. os.replace raises os.rename.
TOOLS = {"mkvpropedit", "mkvmerge", "mkvextract", "ffmpeg"}   # the tools that write a file


def targets(argv):
    """The files a run of a tool of TOOLS writes, from argv: mkvpropedit edits its file, mkvmerge writes its -o,
    mkvextract each ID:PATH, and ffmpeg each path that no -i reads. Another program writes none here. A command line
    in one string is split as the shell splits it."""
    argv = shlex.split(argv) if isinstance(argv, str) else [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in argv]
    at = next((i for i, a in enumerate(argv) if os.path.basename(a) in TOOLS), None)
    if at is None:
        return []
    tool, pairs = os.path.basename(argv[at]), list(zip(argv[at:], argv[at + 1:]))
    if tool == "mkvpropedit":
        return argv[at + 1:at + 2]
    if tool == "mkvmerge":
        return [b for a, b in pairs if a in ("-o", "--output")]
    if tool == "mkvextract":
        return [b.partition(":")[2] for _, b in pairs if ":" in b]
    return [b for a, b in pairs if a != "-i"]


def guarded(path):
    """Whether path is the file of the armed job or one of its sidecars: a name in its folder that is its stem, or its
    stem and a dot and more. That holds the new name of its conversion too."""
    if not isinstance(path, (str, bytes, os.PathLike)):   # a file descriptor
        return False
    folder, name = os.path.split(os.path.abspath(os.fsdecode(path)))
    return folder == STATE["folder"] and (name + ".").startswith(STATE["stem"] + ".")


def audit(event, args):
    """The audit hook of the guard, see sys.addaudithook(). While a job is armed and no allow() runs, it raises Escaped
    for a write on a guarded() path: an open for write, a rename, a replace, a remove, a link, a truncate, a shutil
    move or copy, and a start of a tool of TOOLS. It keeps the first in STATE, so armed() raises it again."""
    if STATE["folder"] is None or STATE["open"]:
        return
    if event == "open":
        paths = args[:1] if len(args) > 2 and isinstance(args[2], int) and args[2] & WRITE else ()
    elif event == "subprocess.Popen":
        paths = targets(args[1])
    else:
        paths = [args[i] for i in MOVES.get(event, ())]
    for p in paths:
        if guarded(p):
            ex = Escaped(f"{event} of {os.fsdecode(p)} ran outside the write gate of a queued plan")
            STATE["escaped"] = STATE["escaped"] or ex
            raise ex


@contextlib.contextmanager
def armed(path):
    """Guard the writes on path and its sidecars while the block runs, as a job that applies a plan runs, see audit().
    The audit hook stays for the life of the process, so the first call installs it. A write the guard failed raises
    Escaped at the end of the block too, so a caller that caught it still fails the job."""
    if not STATE["hooked"]:
        sys.addaudithook(audit)
        STATE["hooked"] = True
    was = {k: STATE[k] for k in ("folder", "stem", "open", "late", "escaped")}
    folder, name = os.path.split(os.path.abspath(path))
    STATE.update(folder=folder, stem=os.path.splitext(name)[0], open=[], late=[], escaped=None)
    try:
        yield
    finally:
        escaped = STATE["escaped"]
        STATE.update(was)
        if escaped:
            raise escaped
