# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The handlers that can take the job's time limit (docs/design.md, "Time limit"). The time limit ends the job, and
content.OutOfTime is a BaseException, so a plain except Exception lets it pass. Only these can take it: an except or
except* whose type holds OutOfTime or BaseException, a bare except, contextlib.suppress() of either, a return, break
or continue that leaves a finally block, and the __exit__ of a context manager that can return a true value. The test
reads each type in its module, and a type it cannot read counts as one that takes the limit. TABLE pins each one with
its file, its function and its kind, so a new one fails this test until someone classes it."""
import ast, builtins, collections, importlib, os

from arr_media_guard.content import OutOfTime as OUT

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "arr_media_guard")
NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
LOOPS = (ast.For, ast.AsyncFor, ast.While)
# The except types that a local name or self reaches, so the test cannot read them. Each is an Exception subclass.
# They are the errors of PyAV, an optional package, and one error of importlib.metadata.
LOCAL = {"av.error.FFmpegError", "av.error.EOFError", "av.error.BlockingIOError", "self.av.error.FFmpegError", "metadata.PackageNotFoundError"}


def own(body):
    """The nodes of the statements body, with nested defs, classes and lambdas left out."""
    stack = list(body)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in ast.iter_child_nodes(n) if not isinstance(c, NESTED))


def value(t, scope):
    """The flat list of the values of except type t in the module namespace scope. None stands for a part the test
    cannot read."""
    if isinstance(t, ast.Tuple):
        return [v for x in t.elts for v in value(x, scope)]
    if ast.unparse(t) in LOCAL:
        return [Exception]
    if isinstance(t, ast.Name):
        return flat(scope.get(t.id, getattr(builtins, t.id, None)))
    return flat(getattr(value(t.value, scope)[0], t.attr, None)) if isinstance(t, ast.Attribute) else [None]


def flat(v):
    """v as a list, with each tuple opened. A tuple constant of a module holds except classes."""
    return [y for x in v for y in flat(x)] if isinstance(v, tuple) else [v]


def takes(vals):
    """Whether an except of the values vals can take OutOfTime: one is OutOfTime, BaseException or no class."""
    return any(not isinstance(v, type) or issubclass(OUT, v) for v in vals)


def leaves(body):
    """Whether a return, or a break or continue of no loop inside body, leaves the statements body."""
    def walk(n, loop):
        if isinstance(n, ast.Return) or isinstance(n, (ast.Break, ast.Continue)) and not loop:
            return True
        return any(walk(c, loop or isinstance(n, LOOPS)) for c in ast.iter_child_nodes(n) if not isinstance(c, NESTED))
    return any(walk(s, False) for s in body)


def kind(vals, body):
    """reraise: the handler ends with a bare raise, and no path leaves it before. catch: its type holds OutOfTime.
    take: else."""
    if body and isinstance(body[-1], ast.Raise) and body[-1].exc is None and not leaves(body):
        return "reraise"
    return "catch" if OUT in vals else "take"


def takers(package=PACKAGE):
    """Counter of (file, function, kind) of each place in the package that can take OutOfTime."""
    out = collections.Counter()
    for f in sorted(x for x in os.listdir(package) if x.endswith(".py")):
        scope = vars(importlib.import_module("arr_media_guard" + ("" if f == "__init__.py" else f".{f[:-3]}")))
        def walk(node, qual):
            for c in ast.iter_child_nodes(node):
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if c.name in ("__exit__", "__aexit__") and any(isinstance(n, ast.Return) and n.value is not None
                                                                  and not (isinstance(n.value, ast.Constant) and not n.value.value) for n in own(c.body)):
                        out[(f, f"{qual}{c.name}", "take")] += 1   # a true value from __exit__ ends the exception
                    walk(c, f"{qual}{c.name}.")
                    continue
                where = qual[:-1] or "<module>"
                if isinstance(c, (ast.Try, ast.TryStar)):
                    for h in c.handlers:
                        vals = value(h.type, scope) if h.type is not None else [BaseException]
                        if takes(vals):
                            out[(f, where, kind(vals, h.body))] += 1
                    if leaves(c.finalbody):
                        out[(f, where, "take")] += 1   # a finally that leaves ends the exception
                if isinstance(c, (ast.With, ast.AsyncWith)):
                    for it in c.items:
                        e = it.context_expr
                        if isinstance(e, ast.Call) and ast.unparse(e.func).split(".")[-1] == "suppress" and \
                                takes([v for a in e.args for v in value(a, scope)]):
                            out[(f, where, "take")] += 1
                walk(c, qual)
        walk(ast.parse(open(os.path.join(package, f)).read()), "")
    return out


def test_each_handler_that_can_take_the_time_limit_is_in_the_table():
    got, want = takers(), collections.Counter(TABLE)
    assert got == want, ("a handler that can take OutOfTime changed. The time limit ends the job, so class it in TABLE. "
                         f"Not in TABLE: {sorted((got - want).elements())}. In TABLE, not in the code: {sorted((want - got).elements())}")


TABLE = [   # (file, function, kind)
    ("checks.py", "video_check", "catch"),
    ("cli.py", "backfill_file", "catch"),
    ("convert.py", "convert", "reraise"),
    ("convert.py", "convert", "reraise"),
    ("convert.py", "convert", "reraise"),
    ("convert.py", "convert", "take"),
    ("convert.py", "convert.ready", "reraise"),
    ("convert.py", "hide_extras", "reraise"),
    ("ff.py", "Av.__exit__", "take"),   # gives back what PyAV's own __exit__ gives, which closes and returns None
    ("health.py", "write", "reraise"),
    ("lid.py", "speech", "reraise"),
    ("process.py", "content_checks", "catch"),
    ("process.py", "quick_check", "catch"),
    ("proof.py", "prove", "reraise"),
    ("regrab.py", "restore_plans", "catch"),
    ("remux.py", "keep_broken", "reraise"),
    ("remux.py", "repack", "take"),
    ("remux.py", "resub", "take"),
    ("runner.py", "job_process", "take"),
    ("runner.py", "run_job", "catch"),
    ("serve.py", "new_password", "reraise"),
    ("store.py", "db", "reraise"),
    ("store.py", "tx", "reraise"),
    ("store.py", "upgrade", "reraise"),
    ("subtitles.py", "side_rename", "reraise"),
    ("vault.py", "keep_grab", "reraise"),
    ("vault.py", "keep_original", "reraise"),
]
