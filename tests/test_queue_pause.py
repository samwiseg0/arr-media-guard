# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The pause of the background queue, see runner.pause(), cli.queue_change() and cli.wait_background(). A pause starts
no new job of the background queue and stops none that runs. The imports run. The pause lives in the state store."""
import io
import os
import shlex
import signal
import subprocess
import sys

import pytest

from test_arr_media_guard import (FILES, background_job, claim_by, claimed, enqueue, env, hook, libraries, log_lines, pool, procs,  # noqa: F401
                                  queue_out, queued_paths, real_wait, run_worker, trace, traced)

PAUSED = "background queue: paused, its jobs wait for --queue --resume"
# A worker that holds worker.lock, see the procs fixture. Its command line names arr-media-guard, so worker_held() finds
# its open lock file. /proc/locks alone can miss a held lock: a read of it over 4 KB takes several reads, and a lock
# that another process drops between them moves the list. A probe missed 152 of 9310 reads under lock churn.
LOCKED = "f = open(sys.argv[-1], 'w')\nfcntl.flock(f, fcntl.LOCK_EX)"


def fake_jobs(monkeypatch):
    """Each job, an import or one of the background queue, traces its start and end and then leaves the queue, in the
    worker and in a job process."""
    def job(name, pending, shared=False, claimed=False):
        trace("start", name=name)
        trace("end", name=name)
        hook.drop_job(name, claimed)
    monkeypatch.setattr(hook, "run_job", job)
    monkeypatch.setattr(hook, "background", job)


def started():
    return [r["name"] for r in traced("start")]


def test_a_paused_queue_claims_no_background_job_and_the_imports_still_start(env, settings, procs):
    """The claim of a job of the background queue fails while the queue is paused, so a list that the worker read just
    before the pause starts nothing. The running job keeps its claim. An import still starts. waiting() starts no worker
    for the background queue alone, and starts one for an import."""
    settings(hook_workers=2, background_workers=2)
    running = claim_by(background_job("/m/a.mkv"), procs().pid)
    waits = background_job("/m/b.mkv", backfill=True)
    assert hook.next_jobs([], [running], lambda x: x) == [waits]
    hook.main(["--queue", "--pause"])
    assert hook.next_jobs([], [running], lambda x: x) == [] and not hook.claim(waits) and claimed() == [running]
    imp = enqueue(env, 0, "/m/i.mkv")
    assert hook.next_jobs([imp], [running], lambda x: x) == [imp] and hook.claim(imp)
    hook.drop_job(running, True)
    hook.drop_job(imp, True)
    assert not hook.waiting()
    enqueue(env, 1, "/m/j.mkv")
    assert hook.waiting()


def test_a_pause_while_a_background_job_runs_lets_it_end_and_the_import_run_in_job_processes(pool, monkeypatch, settings):
    """--queue --pause while a job of the background queue runs: the job ends as it would. An import that comes during
    the pause runs. The next job of the background queue waits, and the worker exits once nothing runs."""
    settings(hook_workers=2, background_workers=1)
    first, second = background_job("/m/a.mkv"), background_job("/m/b.mkv", backfill=True)

    def job(name, pending, shared=False, claimed=False):
        trace("start", name=name)
        if name == first:
            hook.main(["--queue", "--pause"])
            trace("import", name=enqueue(pool, 0, "/m/i.mkv"))
            real_wait(0.3)
        trace("end", name=name)
        hook.drop_job(name, claimed)
    monkeypatch.setattr(hook, "run_job", job)
    run_worker()
    (imp,) = [r["name"] for r in traced("import")]
    assert started() == [first, imp] and sorted(r["name"] for r in traced("end")) == sorted([first, imp]), traced()
    assert hook.deep_analysis_queued() == [second] and claimed() == [] and hook.paused()
    assert [r["note"] for r in log_lines(pool) if r.get("result") == "paused"] == ["the background queue is paused, its jobs wait for --queue --resume"]


def test_the_one_place_worker_runs_the_import_and_leaves_the_background_queue_while_paused(env, monkeypatch, settings, capsys):
    """With HOOK_WORKERS and BACKGROUND_WORKERS at 1 the worker runs each job in itself. While paused it runs the import
    and exits. --queue --resume logs once, and the next worker runs the job of the background queue."""
    settings(hook_workers=1, background_workers=1)
    fake_jobs(monkeypatch)
    deep = background_job("/m/a.mkv")
    hook.main(["--queue", "--pause"])
    imp = enqueue(env, 0, "/m/i.mkv")
    run_worker()
    assert started() == [imp] and hook.deep_analysis_queued() == [deep]
    for _ in range(2):
        hook.main(["--queue", "--resume"])
    assert capsys.readouterr().out.splitlines() == [PAUSED, "background queue: resumed", "background queue: resumed"]
    assert [(r["source"], r["result"]) for r in log_lines(env) if r.get("result") in ("paused", "resumed")] == \
        [("backfill", "paused"), ("backfill", "resumed")]   # a command's line, as monitoring.md names the sources
    assert hook.waiting()
    run_worker()
    assert started() == [imp, deep] and hook.deep_analysis_queued() == []


def test_the_pause_stays_across_a_restart_and_the_next_worker_starts_no_background_job(env, monkeypatch, settings, capsys):
    """The pause is a row in the state store, so a new connection, as after a restart, still reads it. A job a dead
    worker left claimed goes back to the queue at the next worker's start and does not run. --queue and --selftest show
    the pause."""
    settings(hook_workers=2, background_workers=2)
    fake_jobs(monkeypatch)
    dead = subprocess.Popen(["true"])
    dead.wait()
    left = claim_by(background_job("/m/a.mkv", backfill=True), dead.pid)
    hook.main(["--queue", "--pause"])
    hook.main(["--queue", "--pause"])   # a second pause changes nothing, so it logs nothing
    hook.store.close()
    assert hook.paused() and hook.waiting()   # the claimed job starts a worker, which puts it back
    run_worker()
    assert started() == [] and claimed() == [] and hook.deep_analysis_queued() == [left] and not hook.waiting()
    capsys.readouterr()
    out = queue_out(capsys)
    assert out[2:4] == ["background queue: 1 jobs, 1 of them from a backfill (1 file still to check)", PAUSED], out
    hook.main(["--selftest"])
    assert f"background queue: 1 jobs, 1 of them from a backfill (1 file still to check)\n{PAUSED}\n" in capsys.readouterr().out
    assert [r["result"] for r in log_lines(env) if r.get("result") == "paused"] == ["paused"]


def test_the_backfill_jobs_wait_for_the_resume(env, monkeypatch, settings, capsys):
    """The scenario of the tester's report: a backfill queues its files, a pause holds them, and the resume lets them
    run. --drop-queued still drops the jobs while the queue is paused."""
    lists, _ = libraries(env, monkeypatch, settings, {"radarr": ["A.mkv", "B.mkv", "E.mkv"]})
    a, b, e = lists["radarr"]
    fake_jobs(monkeypatch)
    hook.main(["--backfill", "radarr", "--apply"])
    hook.main(["--queue", "--pause"])
    run_worker()
    assert started() == [] and set(queued_paths()) == {a, b, e}
    hook.main(["--queue", "--resume"])
    run_worker()
    assert sorted(started()) == sorted(map(hook.deep_name, (a, b, e))) and queued_paths() == {}
    hook.main(["--backfill", "radarr", "--apply", "--restart"])
    hook.main(["--queue", "--pause"])
    capsys.readouterr()
    hook.main(["--backfill", "radarr", "--drop-queued"])
    assert "dropped 3 jobs that a backfill queued" in capsys.readouterr().out and queued_paths() == {}


@pytest.mark.parametrize("case", ["job process", "starting", "one place"])
def test_wait_returns_once_no_background_job_runs(env, monkeypatch, settings, capsys, procs, case):
    """--queue --pause --wait looks every POLL seconds until no job of the background queue runs: a job process that
    lives, a claimed job with no pid while a worker holds the lock, or, with one place, a worker that runs. A waiting job
    of the queue does not count. A fake sleep ends the running process at the first look. The options take any order."""
    settings(hook_workers=1 if case == "one place" else 2, background_workers=1 if case == "one place" else 2)
    p = procs() if case == "job process" else procs(LOCKED)
    if case != "one place":
        claim_by(background_job("/m/a.mkv"), p.pid if case == "job process" else None)
    background_job("/m/b.mkv", backfill=True)
    sleeps = []

    def end(s):
        sleeps.append(s)
        assert len(sleeps) < 3, "the wait goes on after the job ended"
        p.kill()
        p.wait()
    monkeypatch.setattr(hook.time, "sleep", end)
    hook.main(["--queue", "--wait", "--pause"] if case == "starting" else ["--queue", "--pause", "--wait"])
    assert capsys.readouterr().out.splitlines() == [PAUSED, "waiting for 1 background job to end", "no background job runs"]
    assert sleeps == [hook.POLL]


def test_ctrl_c_ends_the_wait_by_sigint_with_no_traceback(env, monkeypatch, settings, capsys, procs):
    """Ctrl+C during --wait raises KeyboardInterrupt in its sleep. The wait only looks, so it sets SIGINT to its default
    and sends it to itself, with no traceback, and the pause stays. A fake kill records the signal and returns, as a
    blocked SIGINT would, so the wait then exits 130. Every other kill goes through."""
    settings(hook_workers=2, background_workers=2)
    claim_by(background_job("/m/a.mkv"), procs().pid)
    kills, handlers, kill = [], [], os.kill

    def ctrl_c(s):
        raise KeyboardInterrupt

    def fake_kill(pid, sig):
        if (pid, sig) == (os.getpid(), signal.SIGINT):
            kills.append(handlers[-1])
        else:
            kill(pid, sig)
    monkeypatch.setattr(hook.time, "sleep", ctrl_c)
    monkeypatch.setattr(hook.os, "kill", fake_kill)
    monkeypatch.setattr(hook.signal, "signal", lambda sig, h: handlers.append((sig, h)))   # the test process keeps its own handler
    with pytest.raises((SystemExit, KeyboardInterrupt)) as ex:
        hook.main(["--queue", "--pause", "--wait"])
    assert kills == [(signal.SIGINT, signal.SIG_DFL)], kills   # the default handler is in place when the signal goes
    assert ex.type is SystemExit and ex.value.code == 130 and hook.paused()
    assert capsys.readouterr().err == ""


def test_ctrl_c_stops_a_calling_script_before_its_next_command(env, tmp_path, procs):
    """A script runs the wait and then its next command, as a restart would follow it. Ctrl+C sends SIGINT to the
    whole process group of the script. The real launcher dies by SIGINT, so bash stops the script there, and the next
    command never runs. The wait prints no traceback."""
    claim_by(background_job("/m/a.mkv"), procs().pid)
    env_file = tmp_path / "queue.env"
    env_file.write_text(f"STATE_DIR='{hook.CFG.state_dir}'\nLOG='{hook.CFG.log}'\nHOOK_WORKERS='2'\nBACKGROUND_WORKERS='2'\n")
    wait = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.join(FILES, 'arr-media-guard'))} --queue --pause --wait"
    script = subprocess.Popen(["bash", "-c", f"{wait}; echo NEXT-RAN"], env=dict(os.environ, ARR_MEDIA_GUARD_ENV=str(env_file)),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                              preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    try:
        assert [script.stdout.readline() for _ in range(2)] == [PAUSED + "\n", "waiting for 1 background job to end\n"]
        os.killpg(script.pid, signal.SIGINT)
        out, err = script.communicate(timeout=60)
    finally:
        if script.poll() is None:
            os.killpg(script.pid, signal.SIGKILL)
            script.communicate()
    assert script.returncode == -signal.SIGINT and "NEXT-RAN" not in out and err == "", (script.returncode, out, err)


@pytest.mark.parametrize("how", ["resume", "store moved", "during the count"])
def test_wait_exits_2_when_the_pause_ends_during_the_wait(env, monkeypatch, settings, capsys, procs, how):
    """The pause ends while --wait waits: --queue --resume runs in another shell, or a worker start moves a broken store
    aside and starts a new one with no pause. A job can start right after the wait, so the wait exits 2 at the next
    look. The job ends at the same time, so the look that reads no running job reads the pause too. A resume during the
    count of a look still exits 2, because the look reads the pause after the count."""
    settings(hook_workers=2, background_workers=2)
    p = procs()
    claim_by(background_job("/m/a.mkv"), p.pid)
    hook.main(["--queue", "--pause"])

    def end(s):
        if how == "resume":
            hook.main(["--queue", "--resume"])
        else:   # as store.recover() at a worker start, which then opens a new store
            hook.store.close()
            hook.os.rename(hook.store.path(), hook.store.path() + ".corrupt-x")
        p.kill()
        p.wait()
    if how == "during the count":
        count = hook.background_busy

        def resumed():
            hook.main(["--queue", "--resume"])
            p.kill()
            p.wait()
            return count()
        monkeypatch.setattr(hook, "background_busy", resumed)
        monkeypatch.setattr(hook.time, "sleep", lambda s: pytest.fail("the first look ends the wait"))
    else:
        monkeypatch.setattr(hook.time, "sleep", end)
    capsys.readouterr()
    with pytest.raises(SystemExit) as ex:
        hook.main(["--queue", "--wait"])
    out = capsys.readouterr()
    assert ex.value.code == 2 and not hook.paused(), out
    assert out.err == "arr-media-guard: the background queue is no longer paused, so a job can start after the wait.\n", out
    assert "no background job runs" not in out.out, out


def test_a_process_that_exits_during_the_scan_counts_as_ended_and_never_as_unknown(env, settings, procs, monkeypatch):
    """worker_held() reads the open files of each arr-media-guard process. A process that exits between the read of
    its command line and the read of its open files gives PermissionError, as one of another user does. With no command
    line left it counts as ended, so the answer stays False, and a one-place --wait ends. A live process whose open
    files do not read still gives None. The fake read ends the process first, as the kernel race does."""
    settings(hook_workers=1, background_workers=1)
    p, listdir = procs(), os.listdir
    open(os.path.join(hook.CFG.state_dir, "worker.lock"), "w").close()   # a past worker left it, so the scan runs

    def exits(d):
        if d == f"/proc/{p.pid}/fd":
            p.kill()
            p.wait()
            raise PermissionError(13, "Permission denied", d)
        return listdir(d)
    monkeypatch.setattr(hook.os, "listdir", exits)
    hook.main(["--queue", "--pause", "--wait"])
    assert p.returncode is not None and hook.worker_held() is False   # the scan reached the process, and it ended there


@pytest.mark.parametrize("line, held", [("1: FLOCK  ADVISORY  WRITE 4242 00:2e:{ino} 0 EOF", True),
                                        ("1: POSIX  ADVISORY  WRITE 4242 00:2e:{ino} 0 EOF", False),
                                        ("1: FLOCK  ADVISORY  WRITE 4242 00:2e:{ino}7 0 EOF", False)])
def test_worker_held_takes_a_flock_of_worker_lock_from_proc_locks(env, monkeypatch, line, held):
    """worker_held() takes a FLOCK line of /proc/locks with the inode of worker.lock as a running worker, with no
    process that has the file open, as for a worker whose open files do not read. A POSIX lock of the file does not
    count, and neither does a FLOCK of a file whose inode starts with the same digits. A real read of /proc/locks can
    skip a held lock, so a fake read gives a fixed list here."""
    lock = os.path.join(hook.CFG.state_dir, "worker.lock")
    open(lock, "w").close()
    text, real = line.format(ino=os.stat(lock).st_ino) + "\n", open

    def fake_open(path, *a, **k):
        return io.StringIO(text) if path == "/proc/locks" else real(path, *a, **k)
    monkeypatch.setitem(hook.worker_held.__globals__, "open", fake_open)   # the open() of cli.py alone
    assert hook.worker_held() is held


def test_wait_with_no_running_job_returns_at_once_and_refuses_a_queue_that_runs(env, settings, capsys, monkeypatch):
    """A paused queue with no running job ends the wait at once. A queue that runs can start a job right after the wait,
    so --wait refuses it with exit 2, also with no store. --resume creates no store. With one place, a worker whose open
    files do not read exits 1."""
    settings(hook_workers=2, background_workers=2)
    background_job("/m/b.mkv")
    hook.main(["--queue", "--pause"])
    hook.main(["--queue", "--wait"])
    assert capsys.readouterr().out.splitlines() == [PAUSED, "no background job runs"]
    hook.main(["--queue", "--resume"])
    capsys.readouterr()
    for removed in (False, True):
        if removed:
            hook.store.close()
            hook.os.remove(hook.store.path())
        with pytest.raises(SystemExit) as ex:
            hook.main(["--queue", "--wait"])
        assert ex.value.code == 2 and "--wait needs a paused background queue" in capsys.readouterr().err
    hook.main(["--queue", "--resume"])
    assert not hook.os.path.exists(hook.store.path()) and capsys.readouterr().out == "background queue: resumed\n"
    hook.main(["--queue", "--pause"])
    settings(hook_workers=1, background_workers=1)
    monkeypatch.setattr(hook, "worker_held", lambda: None)
    monkeypatch.setattr(hook.time, "sleep", lambda s: pytest.fail("the wait goes on while the worker is unknown"))
    with pytest.raises(SystemExit) as ex:
        hook.main(["--queue", "--wait"])
    assert ex.value.code == "worker: unknown, run this as root or as the worker's user"


@pytest.mark.parametrize("argv", [["--queue", "--wait"], ["--queue", "--pause"], ["--queue", "--resume"], ["--queue", "--pause", "--wait"]])
def test_a_store_that_does_not_read_exits_1_as_in_queue(env, capsys, argv):
    """A store file that reads as no database stops each form with "the store did not read" and exit 1, as --queue
    does, and prints no traceback."""
    hook.store.close()
    for end in ("", "-wal", "-shm"):
        if hook.os.path.exists(hook.store.path() + end):
            hook.os.remove(hook.store.path() + end)
    with open(hook.store.path(), "wb") as f:
        f.write(b"not a database " * 300)
    with pytest.raises(SystemExit) as ex:
        hook.main(argv)
    assert str(ex.value.code).startswith("the store did not read: "), ex.value.code


@pytest.mark.parametrize("argv", [["--queue", "--pause", "--resume"], ["--queue", "--wait", "--wait"], ["--queue", "--stop"]])
def test_another_queue_option_prints_the_help(env, capsys, argv):
    with pytest.raises(SystemExit) as ex:
        hook.main(argv)
    assert ex.value.code == 2 and "--queue --pause [--wait]" in capsys.readouterr().out and not hook.paused()
