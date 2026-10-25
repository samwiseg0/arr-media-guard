# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Queued plans, see cli.plan_queue(), cli.check_queue() and cli.planned_file(). --background puts each file of an audited
plan in the background queue with its plan row. The worker plans the file again in a dry run, and applies only when
that plan equals the row and the file has the row's key. The apply compares again before each write, see
gate.allow(). The flow tests fake the tools, see the env fixture."""
import copy
import datetime
import json
import os
import sqlite3

import pytest

from test_arr_media_guard import (HEADER_HP, NOT_MATROSKA, QUICK, burn, check_listed, decided, enqueue, env, header_issue, hearing, hook,  # noqa: F401
                                  japanese_film, late_english_film, log_lines, mk, mp4_import, one_alert_film, posted, queue_lines, sub_probe, talk,
                                  verdict)

ENGLISH_FIRST = {"container": {"properties": {"duration": 7200 * 10**9}},   # English plays first, so no flag edit
                 "tracks": [mk("video", "und", 1, True), mk("audio", "eng", 2, True, audio_channels=6), mk("subtitles", "eng", 3, False)]}


def listed(env, monkeypatch, *more):
    """Radarr lists the env's film as movie 7 with file 70, and each path of more as one more movie. Returns the paths."""
    movie = dict(env["movies"]["movie/7"], id=7, movieFile={"id": 70, "movieId": 7, "path": env["path"], "mediaInfo": {"audioStreamCount": 2}})
    env["movies"]["movie"] = [movie] + [dict(movie, id=8 + i, title=f"Film {i}", movieFile={"id": 80 + i, "movieId": 8 + i, "path": p,
                                                                                             "mediaInfo": {"audioStreamCount": 2}})
                                        for i, p in enumerate(more)]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    return [env["path"], *more]


def dry_run(tmp_path, *argv):
    """A dry flag backfill of radarr that writes its plans. Returns the plan file and its rows."""
    plans = tmp_path / "plans" / "radarr.jsonl"
    hook.main(["--backfill", "radarr", "--plan-out", str(plans), *argv])
    return plans, [json.loads(x) for x in plans.read_text().splitlines()]


def queue(plans, *argv):
    hook.main(["--backfill", "radarr", "--apply", "--plan-from", str(plans), "--background", *argv])


def lines(env, source="backfill_job"):
    return [r for r in log_lines(env) if "schema" in r and r["source"] == source]


def test_a_queued_plan_applies_in_the_worker_as_the_flag_backfill(env, monkeypatch, tmp_path, capsys):
    """Tester 2026-10-20: a dry run, an audit, then the apply, in the worker's places. The queue run edits nothing. The
    worker plans the file again, finds the same plan, and applies it as the flag backfill does: no audio check, and no
    job queued after it."""
    (path,) = listed(env, monkeypatch)
    plans, (row,) = dry_run(tmp_path)
    assert row["key"] == list(hook.file_key(os.stat(path))) and row["edits"], row
    capsys.readouterr()
    queue(plans)
    name = hook.deep_name(path)
    assert "radarr: 1 file queued with the plan of the dry run, 2.0 hours of video." in capsys.readouterr().out
    job = hook.job_of(name)
    assert (job["plan"], job["flag_backfill"], job["backfill"]) == (row, True, True) and env["mkvpropedit"] == []
    hook.background(name, [])
    (rec,) = lines(env)
    assert (rec["outcome"], rec["apply"], rec["job"]) == ("edited", True, name) and "audio" not in rec, rec
    assert len(env["mkvpropedit"]) == 1 and hook.deep_analysis_queued() == [] and hook.store.read("SELECT name FROM jobs") == []


def test_a_file_that_changed_since_the_dry_run_never_queues_and_the_worker_refuses_one(env, monkeypatch, tmp_path, capsys):
    """A file whose key is not the plan row's does not queue. A file that changes while its job waits gets a decision
    line that says why, and no edit and no post. The check runs under the file lock in each run, see process.start()."""
    (path,) = listed(env, monkeypatch)
    plans, _ = dry_run(tmp_path)
    os.utime(path, ns=(1, 1))
    queue(plans)
    assert "radarr: 1 file of the plan not queued, the file changed since the dry run\n" in capsys.readouterr().out
    assert hook.deep_analysis_queued() == [] and queue_lines(env) == [("radarr", "backfill", 0, 0, 1, {"plan_from": str(plans)})]
    assert log_lines(env)[-1]["refused_why"] == {"the file changed since the dry run": 1}
    plans, _ = dry_run(tmp_path)
    queue(plans)
    with open(path, "ab") as f:
        f.write(b"y")
    hook.background(hook.deep_name(path), [])
    (rec,) = lines(env)
    assert (rec["outcome"], rec["result"], rec["apply"]) == ("plan_refused", "refused, the file changed since the dry run", True), rec
    assert env["mkvpropedit"] == [] and posted(env) == [] and hook.deep_analysis_queued() == [] and "class" not in rec


def test_a_plan_that_differs_from_the_dry_runs_is_refused(env, monkeypatch, tmp_path):
    """The app names another original language after the dry run, so the new plan differs. The worker refuses the file
    before any write. Its line names each field that differs, with both values."""
    (path,) = listed(env, monkeypatch)
    plans, (row,) = dry_run(tmp_path)
    queue(plans)
    name = hook.deep_name(path)
    job = hook.job_of(name)
    hook.put_job(name, dict(job, inputs=dict(job["inputs"], original="Portuguese")))
    hook.background(name, [])
    (rec,) = lines(env)
    assert rec["outcome"] == "plan_refused" and rec["result"].startswith("refused, the plan differs from the dry run's in "), rec
    assert "edits" in rec["plan_diff"] and rec["plan_diff"]["edits"][0] == row["edits"] and "label" not in rec["plan_diff"], rec
    assert env["mkvpropedit"] == [] and posted(env) == [] and "class" not in rec


def test_a_second_queue_run_queues_no_double_limit_counts_and_drop_queued_drops(env, monkeypatch, tmp_path, capsys):
    other = os.path.join(os.path.dirname(env["path"]), "Other.mkv")
    with open(other, "wb") as f:
        f.write(b"z" * 10)
    paths = listed(env, monkeypatch, other)
    plans, rows = dry_run(tmp_path)
    assert [r["path"] for r in rows] == paths
    queue(plans, "--limit", "1")
    assert [hook.job_of(n)["path"] for n in hook.deep_analysis_queued()] == paths[:1]
    capsys.readouterr()
    queue(plans)
    assert "radarr: 1 file queued with the plan of the dry run, 2.0 hours of video, 1 kept the job of its path" in capsys.readouterr().out
    assert sorted(hook.job_of(n)["path"] for n in hook.deep_analysis_queued()) == sorted(paths)
    hook.main(["--backfill", "radarr", "--drop-queued"])
    assert "dropped 2 jobs that a backfill queued" in capsys.readouterr().out and hook.deep_analysis_queued() == []


def fail_queue(monkeypatch, at, error):
    """runner.queue_listed() raises error at its call number at."""
    real, calls = hook.queue_listed, [0]
    def queue_listed(*a, **k):
        calls[0] += 1
        if calls[0] == at:
            raise error()
        return real(*a, **k)
    monkeypatch.setattr(hook, "queue_listed", queue_listed)


@pytest.mark.parametrize("error", [sqlite3.OperationalError, KeyboardInterrupt])
@pytest.mark.parametrize("plan", [True, False])
def test_a_queue_run_that_fails_part_way_writes_its_line(env, monkeypatch, tmp_path, error, plan):
    """Reviewer 2026-10-24: a plan queue run whose second file raised "database is locked" wrote no queue line, and the
    job of its first file waited in the queue. A plan queue and a bare backfill now write the line with the counts so
    far, as runner.queue_rechecks() writes its line after a failure. Ctrl+C gives the same line."""
    other = os.path.join(os.path.dirname(env["path"]), "Other.mkv")
    with open(other, "wb") as f:
        f.write(b"z" * 10)
    listed(env, monkeypatch, other)
    plans, _ = dry_run(tmp_path)
    fail_queue(monkeypatch, 2, error)
    with pytest.raises(error):
        queue(plans) if plan else hook.main(["--backfill", "radarr", "--apply"])
    options = {"plan_from": str(plans)} if plan else {}
    assert len(hook.deep_analysis_queued()) == 1 and queue_lines(env) == [("radarr", "backfill", 1, 0, 0, options)]


@pytest.mark.parametrize("error", [sqlite3.OperationalError, KeyboardInterrupt])
def test_a_check_queue_that_fails_writes_its_line(env, monkeypatch, tmp_path, error):
    """--check --apply --background writes its queue line when runner.queue_listed() raises, as the backfill queues do."""
    check_listed(env, monkeypatch)
    plans = tmp_path / "check.jsonl"
    hook.main(["--check", env["path"], "--plan-out", str(plans)])
    fail_queue(monkeypatch, 1, error)
    with pytest.raises(error):
        hook.main(["--check", env["path"], "--apply", "--plan-from", str(plans), "--background"])
    assert queue_lines(env) == [("radarr", "check", 0, 0, 0, {"paths": [env["path"]], "plan_from": str(plans)})]


def test_each_queue_run_writes_a_line_and_each_job_line_names_its_queue_time(env, monkeypatch, tmp_path):
    """Tester 2026-10-22: the edit of two files failed, so neither file changed, and each plan queue run queued both
    again. Each apply of a queue command writes one line with its counts and its options. The line of each job names
    the time of its queue in "queued", so two runs of one job name show two queue times."""
    other = os.path.join(os.path.dirname(env["path"]), "Other.mkv")
    with open(other, "wb") as f:
        f.write(b"z" * 10)
    paths = listed(env, monkeypatch, other)
    plans, rows = dry_run(tmp_path)
    real = hook.subprocess.run
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: type("R", (), {"returncode": 2, "stdout": "", "stderr": "bad"})()
                        if argv[0] == "mkvpropedit" else real(argv, **kw))
    times = []
    for _ in range(2):
        env["clock"][0] += 3600
        times.append(datetime.datetime.fromtimestamp(env["clock"][0]).astimezone().isoformat(timespec="seconds"))
        queue(plans)
        for p in paths:
            hook.background(hook.deep_name(p), [])
    assert queue_lines(env) == [("radarr", "backfill", 2, 0, 0, {"plan_from": str(plans)})] * 2
    assert sorted((r["path"], r["queued"], r["outcome"]) for r in lines(env)) == sorted((p, t, "edit_failed") for p in paths for t in times)
    assert [list(hook.file_key(os.stat(p))) for p in paths] == [r["key"] for r in rows] and times[0] != times[1]
    assert "queued" not in {json.loads(r).get("result") for (r,) in hook.store.read("SELECT rec FROM decisions")}   # the audits read these rows


def test_imports_go_first_and_background_workers_caps_the_queued_plans(env, monkeypatch, settings, tmp_path):
    paths = [env["path"]]
    for i in range(2):
        paths.append(os.path.join(os.path.dirname(env["path"]), f"Film {i}.mkv"))
        with open(paths[-1], "wb") as f:
            f.write(b"x")
    listed(env, monkeypatch, *paths[1:])
    plans, _ = dry_run(tmp_path)
    queue(plans)
    settings(hook_workers=1, background_workers=2)
    queued = hook.deep_analysis_queued()
    assert len(queued) == 3 and hook.next_jobs([], [], lambda x: x) == queued[:2]
    waits = enqueue(env, 0, env["path"])
    assert hook.next_jobs(hook.queued(), [], lambda x: x) == [waits]


def test_a_queued_header_repair_yields_to_an_import_then_repairs_and_rescans(env, monkeypatch, tmp_path):
    """A job of a flag backfill plan stops for an import that waits before its remux, as a job a backfill queued does.
    Its next run repairs the header and sends the rescan of the item, since no end of a backfill run follows."""
    calls = header_issue(env, monkeypatch)
    env["probe"] = copy.deepcopy(ENGLISH_FIRST)
    (path,) = listed(env, monkeypatch)
    plans, (row,) = dry_run(tmp_path)
    assert (row["header_repair"], row["edits"]) == ("would_repair_header", []), row
    queue(plans)
    name, waits = hook.deep_name(path), enqueue(env, 0, path)
    hook.background(name, [])
    assert [r["result"] for r in log_lines(env) if r.get("job") == name] == ["yielded"] and True not in calls["repairs"]
    assert hook.deep_analysis_queued() == [name]
    hook.drop_job(waits)
    hook.background(name, [])
    assert calls["repairs"][-1] is True and lines(env)[-1]["header_repair"]["code"] == "header_repaired"
    assert env["writes"] == [("POST", "command", {"name": "RescanMovie", "movieId": 7})]


def test_check_queues_its_audited_plan_and_refuses_what_it_cannot_queue(env, monkeypatch, tmp_path, capsys):
    """--check --plan-out writes the plan of each file. --check --apply --plan-from --background queues it, and the
    worker applies it as --check --apply does, with the checks of an import. A path no app lists and a plan row with no
    file key do not queue, and the exit code is 1."""
    check_listed(env, monkeypatch)
    plans = tmp_path / "check.jsonl"
    hook.main(["--check", env["path"], "--plan-out", str(plans)])
    (row,) = [json.loads(x) for x in plans.read_text().splitlines()]
    assert row["key"] == list(hook.file_key(os.stat(env["path"]))) and row["edits"] and env["mkvpropedit"] == []
    hook.main(["--check", env["path"], "--apply", "--plan-from", str(plans), "--background"])
    name = hook.deep_name(env["path"])
    assert "flag_backfill" not in hook.job_of(name) and env["mkvpropedit"] == [] and "1 file queued with the plan" in capsys.readouterr().out
    with pytest.raises(SystemExit):   # the job of the path waits, so the file keeps it
        hook.main(["--check", env["path"], "--apply", "--plan-from", str(plans), "--background"])
    options = {"paths": [env["path"]], "plan_from": str(plans)}
    assert queue_lines(env) == [("radarr", "check", 1, 0, 0, options), ("radarr", "check", 0, 1, 0, options)]
    assert hook.claim(name)   # --queue names a claimed job by the source of its lines
    hook.main(["--queue"])
    assert f"  {'check_job':<14}{'process gone':<27}{env['path']}" in capsys.readouterr().out.splitlines()
    hook.requeue(name)
    hook.background(name, [])
    (rec,) = lines(env, "check_job")
    assert rec["outcome"] == "edited" and "audio" in rec and len(env["mkvpropedit"]) == 1, rec
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    plans.write_text(json.dumps({k: v for k, v in row.items() if k != "key"}) + "\n")
    with pytest.raises(SystemExit) as ex:
        hook.main(["--check", env["path"], str(outside), "--apply", "--plan-from", str(plans), "--background"])
    out = capsys.readouterr().out
    assert f"not queued, the plan holds no file key, so run the dry run again with this version: {env['path']}" in out, out
    assert f"not queued, no app lists it, so the background queue cannot take it: {outside}" in out and "0 files queued" in out
    assert ex.value.code.startswith("--check did not queue every file") and hook.deep_analysis_queued() == []
    assert queue_lines(env)[-1][2:5] == (0, 0, 2) and log_lines(env)[-1]["refused_why"] == {
        "the plan holds no file key, so run the dry run again with this version": 1, "no app lists it, so the background queue cannot take it": 1}
    (tmp_path / "outside2.mkv").write_bytes(b"x")
    with pytest.raises(SystemExit):   # no app lists the two files, so the line names no app
        hook.main(["--check", str(outside), str(tmp_path / "outside2.mkv"), "--apply", "--plan-from", str(plans), "--background"])
    assert queue_lines(env)[-1][:5] == (None, "check", 0, 0, 2)


def test_a_queued_check_keeps_the_original_of_a_conversion(env, monkeypatch, settings, tmp_path):
    """Owner 2026-10-21: a job of --check --background runs as the queued form of --check. Its conversion keeps the
    original for KEEP_ORIGINALS_DAYS, as --check --apply does, and its line has source check_job."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    check_listed(env, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    plans, before = tmp_path / "check.jsonl", open(mp4, "rb").read()
    hook.main(["--check", mp4, "--plan-out", str(plans)])
    hook.main(["--check", mp4, "--apply", "--plan-from", str(plans), "--background"])
    assert os.path.exists(mp4) and env["repacks"] == []
    hook.background(hook.deep_name(mp4), [])
    (rec,) = lines(env, "check_job")
    assert (rec["path"], rec["reasons"][0]) == (mkv, "repacked") and not os.path.exists(mp4), rec
    assert open(rec["repack"]["kept"], "rb").read() == before


@pytest.mark.parametrize("argv, said", [(["--backfill", "radarr", "--apply", "--background"], "--background needs --apply and --plan-from"),
                                        (["--backfill", "radarr", "--plan-from", "p", "--background"], "--background needs --apply and --plan-from"),
                                        (["--backfill", "radarr", "--apply", "--plan-from", "p", "--background", "--only-undecided"], "--background takes no"),
                                        (["--check", "/m/A.mkv", "--apply", "--background"], "--background needs --apply and --plan-from"),
                                        (["--check", "/m/A.mkv", "--plan-from", "p"], "--plan-from needs --background"),
                                        (["--check", "/m/A.mkv", "--apply", "--plan-from", "p", "--background", "--plan-out", "q"], "--plan-out writes no")])
def test_background_needs_an_apply_of_a_plan_file(env, capsys, argv, said):
    with pytest.raises(SystemExit) as ex:
        hook.main(argv)
    assert ex.value.code == 2 and said in capsys.readouterr().err


def test_plan_diff_leaves_out_the_label_the_path_and_the_key():
    row = {"app": "radarr", "label": "A", "path": "/m/A.mkv", "key": [1, 2, 3], "edits": [[2, 1, 0]], "class": "c"}
    assert hook.plan_diff(row, dict(row, label="B", path="/m/B.mkv", key=[4, 5, 6], edits=[(2, 1, 0)])) == {}
    assert hook.plan_diff(row, dict(row, edits=[], header_repair="would_repair_header")) == {"edits": [[[2, 1, 0]], []],
                                                                                           "header_repair": [None, "would_repair_header"]}
    assert set(hook.plan_diff(row, None)) == {"app", "class", "edits"}


def check_plan(env, tmp_path, path=None):
    """A dry --check of the env's film with --plan-out, then its queue run. Returns the row."""
    plans = tmp_path / "check.jsonl"
    hook.main(["--check", path or env["path"], "--plan-out", str(plans)])
    (row,) = [json.loads(x) for x in plans.read_text().splitlines()]
    hook.main(["--check", path or env["path"], "--apply", "--plan-from", str(plans), "--background"])
    return row


def outside(then):
    """Run then() as another program would, so the guard of the write gate sees none of its writes, see gate.armed()."""
    was = hook.STATE["folder"]
    hook.STATE["folder"] = None
    try:
        then()
    finally:
        hook.STATE["folder"] = was


def in_the_apply(monkeypatch, then):
    """Run then() as the apply pass of a queued job starts, after the compare of its dry pass. Another program makes
    its changes, see outside()."""
    real = hook.backfill_file

    def bf(app, f, info, a, keep_plan, *more, **kw):
        if a.apply:
            outside(then)
        return real(app, f, info, a, keep_plan, *more, **kw)
    monkeypatch.setattr(hook, "backfill_file", bf)


def test_the_apply_refuses_flag_edits_that_the_row_does_not_hold(env, monkeypatch, tmp_path):
    """Reviewer F1 2026-10-21: the dry runs hear nothing, as after a time limit, and the apply hears English. The apply
    pass decides new flag edits. It compares them with the row before its write, and refuses."""
    env["probe"] = {"container": {"properties": {"duration": 7200 * 10**9}},
                    "tracks": [mk("video", "und", 1, True), mk("audio", "und", 2, True, audio_channels=6), mk("subtitles", "eng", 3, True)]}
    heard = {"apply": False}
    in_the_apply(monkeypatch, lambda: heard.update(apply=True))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "hear", lambda path, j, d, original, fresh=False, only=None, keep=False, then=None: (
        {"a1": "eng"}, {"a1": {"lang": "eng", "prob": 0.99, "why": None, "cached": False, "took": 1}}) if heard["apply"] else (
        {}, {"a1": {"lang": None, "prob": None, "why": "no time left", "cached": None, "took": 0}}))
    check_listed(env, monkeypatch)
    row = check_plan(env, tmp_path)
    assert row["edits"] == [] and row["class"] == "no change", row
    hook.background(hook.deep_name(env["path"]), [])
    (rec,) = lines(env, "check_job")
    assert (rec["outcome"], rec["result"]) == ("plan_refused", "refused, the plan differs from the dry run's in edits"), rec
    assert rec["plan_diff"]["edits"][0] == [] and env["mkvpropedit"] == [] and posted(env) == [] and "class" not in rec


def test_a_check_row_of_an_mp4_holds_its_conversion(env, monkeypatch, tmp_path, capsys):
    """Reviewer F2 2026-10-21: the row of a file that is not .mkv holds its conversion, so the audit counts it."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    check_listed(env, monkeypatch)
    plans = tmp_path / "check.jsonl"
    hook.main(["--check", mp4, "--plan-out", str(plans)])
    (row,) = [json.loads(x) for x in plans.read_text().splitlines()]
    capsys.readouterr()
    hook.main(["--audit", "radarr", "--plan-from", str(plans)])
    assert (row["outcome"], row["repack"].startswith("would repack: "), len(row["convert_plan"])) == ("would_repack", True, 16), row
    assert "1 would be repacked into Matroska" in capsys.readouterr().out


def test_the_jobs_own_conversion_then_takes_the_flag_edits_of_the_new_file(env, monkeypatch, tmp_path):
    """Owner 2026-10-22, "convert and edit": the row of a .mkv file that holds MP4 plans the conversion and no flag edit,
    as no dry run sees the new file. After the job's own conversion the new file gets the flag edits, as the foreground
    apply gives them."""
    env["probe"] = copy.deepcopy(NOT_MATROSKA["probe"])
    for i, t in enumerate(env["probe"]["tracks"]):
        t["id"] = i
    (path,) = listed(env, monkeypatch)
    plans, (row,) = dry_run(tmp_path)
    assert (row["outcome"], row["edits"]) == ("would_repack", []), row
    queue(plans)
    hook.background(hook.deep_name(path), [])
    (rec,) = lines(env)
    assert (rec["outcome"], rec["reasons"][0], len(env["repacks"]), len(env["mkvpropedit"])) == ("edited", "repacked", 1, 1), rec


def test_a_conversion_that_differs_from_the_rows_refuses_before_any_write(env, monkeypatch, tmp_path):
    """A sidecar comes beside the file after the dry pass. The conversion would mux it, which the row does not hold, so
    the conversion refuses before it writes anything, and the sidecar stays."""
    env["probe"] = copy.deepcopy(NOT_MATROSKA["probe"])
    for i, t in enumerate(env["probe"]["tracks"]):
        t["id"] = i
    (path,) = listed(env, monkeypatch)
    plans, _ = dry_run(tmp_path)
    queue(plans)
    side = path[:-4] + ".en.srt"
    in_the_apply(monkeypatch, lambda: open(side, "w").write("1\n00:00:01,000 --> 00:00:02,000\nHello\n"))
    hook.background(hook.deep_name(path), [])
    (rec,) = lines(env)
    assert (rec["outcome"], list(rec["plan_diff"])) == ("plan_refused", ["convert_plan"]) and env["repacks"] == [] and os.path.exists(side), rec


@pytest.mark.parametrize("first", [True, False])
def test_a_header_repair_that_differs_from_the_rows_refuses(env, monkeypatch, tmp_path, first):
    """The row holds what the header repair writes. The apply pass finds another issue, so it refuses before the first
    repair that the checks need, or before the one write."""
    calls = header_issue(env, monkeypatch)
    hp = dict(HEADER_HP, first=first)
    monkeypatch.setattr(hook, "header_of", lambda path, j=None: copy.deepcopy(hp))
    env["probe"] = copy.deepcopy(ENGLISH_FIRST)
    (path,) = listed(env, monkeypatch)
    plans, (row,) = dry_run(tmp_path)
    assert (row["header_repair"], len(row["header_plan"])) == ("would_repair_header", 16), row
    queue(plans)
    in_the_apply(monkeypatch, lambda: hp.update(issue=["the Cues are missing"]))
    hook.background(hook.deep_name(path), [])
    (rec,) = lines(env)
    assert (rec["outcome"], list(rec["plan_diff"])) == ("plan_refused", ["header_plan"]) and True not in calls["repairs"], rec


def test_the_header_plan_holds_the_runtime_that_decides_a_removal(env, monkeypatch, tmp_path):
    """Reviewer F4 2026-10-21: a removal of late subtitle lines takes the item's runtime. The app gives another runtime
    after the dry run, so the dry pass of the job plans another repair and refuses."""
    header_issue(env, monkeypatch)
    monkeypatch.setattr(hook, "header_of", lambda path, j=None: dict(HEADER_HP, trim=[3], remove=[], subtitles={3: 7000.0}, streams=6873.4))
    env["probe"] = copy.deepcopy(ENGLISH_FIRST)
    (path,) = listed(env, monkeypatch)
    plans, (row,) = dry_run(tmp_path)
    assert row["header_repair"] == "would_repair_header", row
    queue(plans)
    name = hook.deep_name(path)
    job = hook.job_of(name)
    hook.put_job(name, dict(job, inputs=dict(job["inputs"], runtime=job["inputs"]["runtime"] - 1)))
    hook.background(name, [])
    (rec,) = lines(env)
    assert (rec["outcome"], list(rec["plan_diff"])) == ("plan_refused", ["header_plan"]), rec


def test_a_changed_retime_amount_refuses(env, monkeypatch, tmp_path):
    """Reviewer F4 2026-10-21: the row holds the new times of a retime, not only the track. The job's fit finds 4 s
    where the dry run found 2 s, so the job refuses, with no remux."""
    got = late_english_film(env, monkeypatch)
    check_listed(env, monkeypatch)
    plans = tmp_path / "c.jsonl"
    hook.main(["--check", env["path"], "--plan-out", str(plans)])
    hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=4.0)})   # the file key stays
    hook.main(["--check", env["path"], "--apply", "--plan-from", str(plans), "--background"])
    hook.background(hook.deep_name(env["path"]), [])
    (rec,) = lines(env, "check_job")
    assert (rec["outcome"], list(rec["plan_diff"]), [a for a, _, _ in got]) == ("plan_refused", ["subtitles"], [False, False]), rec


def test_a_change_between_the_passes_refuses(env, monkeypatch, tmp_path):
    """Reviewer F5 2026-10-21: the file changes after the dry pass of the job compared. The apply pass takes the file
    key under the file lock, and refuses."""
    (path,) = listed(env, monkeypatch)
    plans, _ = dry_run(tmp_path)
    queue(plans)
    in_the_apply(monkeypatch, lambda: open(path, "ab").write(b"y"))
    hook.background(hook.deep_name(path), [])
    (rec,) = lines(env)
    assert (rec["outcome"], rec["result"], env["mkvpropedit"]) == ("plan_refused", "refused, the file changed since the dry run", [])


@pytest.mark.parametrize("same", [True, False])
def test_the_deep_analysis_of_a_queued_check_follows_the_rows_plan(env, monkeypatch, settings, tmp_path, same):
    """Owner 2026-10-22: the deep analysis that a queued --check queues follows the plan of the dry --check. The row
    holds what its deep analysis writes. The job turns the English subtitles off first, which the deep analysis keeps.
    A deep analysis that plans the same moves the block of lines. One that plans otherwise refuses, writes nothing, and
    the alert the check held for it posts."""
    one_alert_film(env, monkeypatch, settings, "fixed")
    env["probe"] = sub_probe(("eng", True, {}), audio=(("eng", True),))
    check_listed(env, monkeypatch)
    row = check_plan(env, tmp_path)
    assert row["edits"] and row["deep"]["edits"] == [] and row["deep"]["subtitles"]["timed"] == ["s1"], row
    name = hook.deep_name(env["path"])
    hook.background(name, [])
    job = hook.job_of(name)   # the deep analysis the job queued, under the same name
    assert job["audited"] == row["deep"] and "backfill" not in job, job
    if not same:
        hook.put_job(name, dict(job, audited=dict(row["deep"], subtitles=dict(row["deep"]["subtitles"], times="0" * 16))))
    hook.background(name, [])
    deep = [r for r in log_lines(env) if r.get("source") == "deep_analysis"][-1]
    if same:
        assert (deep["outcome"], deep["subremux"]["done"], posted(env)) == ("subtitles_remuxed", True, []), deep
    else:
        assert (deep["outcome"], list(deep["plan_diff"]), "subremux" in deep) == ("plan_refused", ["subtitles"], False), deep
        assert [t for t, _ in posted(env)] == ["Subtitles out of sync"]


@pytest.mark.parametrize("planned", [True, False])
def test_the_burn_in_job_of_a_queued_check_mutes_only_as_the_row_plans(env, burn, monkeypatch, tmp_path, planned):
    """Owner 2026-10-22: the burn-in job that a queued --check queues follows the plan of the dry --check too, as its
    mute edits the flags. English text over Japanese audio turns the English subtitles off. A dry run that found no
    burned-in text plans no mute, so the job refuses the mute and posts nothing."""
    japanese_film(env, ("eng", True, {}))
    burn["full"] = [verdict("full", True, "eng") if planned else verdict("none", None, None)]
    check_listed(env, monkeypatch)
    row = check_plan(env, tmp_path)
    assert [e[1:] for e in row["burn_in"]["edits"]] == ([[0, 1]] if planned else []), row
    burn["full"] = [verdict("full", True, "eng")]
    hook.background(hook.deep_name(env["path"]), [])
    name = hook.burn_name(env["path"])
    assert hook.job_of(name)["audited"] == row["burn_in"]
    edits = len(env["mkvpropedit"])
    hook.background(name, [])
    b = [r for r in log_lines(env) if r.get("source") == "burn_in"][-1]
    if planned:
        assert (b["edit_result"], len(env["mkvpropedit"]), hook.muted(env["path"])) == ("edited", edits + 1, True), b
    else:
        assert (b["outcome"], len(env["mkvpropedit"]), hook.muted(env["path"])) == ("plan_refused", edits, False), b
        assert "Burned-in subtitles" not in [t for t, _ in posted(env)]


@pytest.mark.parametrize("kind", ["check", "backfill"])
def test_a_stop_after_the_jobs_own_conversion_goes_on_from_the_new_file(env, monkeypatch, settings, tmp_path, kind):
    """Reviewer F6 2026-10-21: a stop comes right after the job's own conversion. The job keeps the key of the new file,
    so its next run knows the change is its own. That run goes on from the new file as after the conversion, its line
    says so, and a job of --check queues the follow-up jobs of an import, with no plan, as no dry run saw that file."""
    if kind == "check":
        path, new = mp4_import(env, monkeypatch, sidecars=())
        check_listed(env, monkeypatch)
        settings(keep_days=7, subtitles="deep")
        monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
        check_plan(env, tmp_path, path)
    else:
        env["probe"] = copy.deepcopy(NOT_MATROSKA["probe"])
        for i, t in enumerate(env["probe"]["tracks"]):
            t["id"] = i
        (path,) = new, = listed(env, monkeypatch)
        queue(dry_run(tmp_path)[0])
    name, real = hook.deep_name(path), hook.convert

    def convert(*a, **k):   # the stop comes as the conversion ends, before the next step
        out = real(*a, **k)
        hook.STOP.update(asked=a[4])
        return out
    monkeypatch.setattr(hook, "convert", convert)
    assert hook.claim(name)
    hook.background(name, [], claimed=True)
    hook.STOP.update(asked=False, skipped=False)
    job = hook.job_of(name, True)
    assert (job["path"], job["converted"]) == (new, list(hook.file_key(os.stat(new)))), job
    hook.requeue(name)
    hook.background(name, [])
    rec = lines(env, "check_job" if kind == "check" else "backfill_job")[-1]
    assert (rec["outcome"], rec["note"], len(env["repacks"])) == ("edited", "this job converted the file before a stop", 1), rec
    later = [hook.job_of(n) for n in hook.deep_analysis_queued()]
    assert [("audited" in j, "backfill" in j, j["path"]) for j in later] == ([(False, False, new)] if kind == "check" else []), later


@pytest.mark.parametrize("source, kinds", [("backfill", ("backfill", "backfill_job")), ("check", ("check", "check_job"))])
def test_audit_source_takes_the_jobs_that_a_backfill_or_check_queued(env, source, kinds):
    """Reviewer F7 2026-10-21: --audit --source backfill counts the lines of the jobs a backfill queued, and --source
    check those of a queued --check."""
    hook.main([])   # the hook edits Film A
    ((at, raw),) = hook.store.read("SELECT at, rec FROM decisions")
    for i, k in enumerate(kinds):   # the edit as a line of each source, each of its own file
        p = env["path"][:-4] + f" {i}.mkv"
        hook.store.write("INSERT INTO decisions VALUES (?, ?, ?, ?)", at, "radarr", p, json.dumps(dict(json.loads(raw), source=k, apply=True, path=p)))
    hook.main(["--audit", "radarr", "--since", "24h", "--source", source])
    assert " edited=2 " in env["syslog"][-1], env["syslog"][-1]


@pytest.mark.parametrize("then, refused", [("fixed", True), ("left", False)])
def test_a_follow_up_job_with_an_empty_plan_refuses_only_a_write(env, monkeypatch, settings, then, refused):
    """The dry --check planned no deep analysis, so the job's deep analysis holds an empty plan. A deep analysis that
    would move a block of lines refuses. One that writes nothing runs as before."""
    one_alert_film(env, monkeypatch, settings, then)
    check_listed(env, monkeypatch)
    rec = {"path": env["path"], "ids": {"app_id": "7", "file_id": None}, "tracks": [{"i": "s1"}]}
    name = hook.queue_deep_analysis({"app": "radarr", "owner": "7"}, rec, {"label": "Film A (1979)", "original": "English", "runtime": 120,
                                                                         "want": None, "kids": False, "ctx": {"listed": 120}, "release": ""}, audited={})
    hook.background(name, [])
    deep = [r for r in log_lines(env) if r.get("source") == "deep_analysis"][-1]
    assert (deep["outcome"] == "plan_refused", list(deep.get("plan_diff") or {})) == (refused, ["subtitles"] if refused else []), deep


def test_a_conversion_refuses_before_it_moves_a_sidecar_aside(env, monkeypatch, settings, tmp_path):
    """The subtitle check of a conversion hears a match in the dry runs and a mismatch in the apply. The conversion
    compares its verdicts with the row's before it moves the sidecar aside, so the sidecar stays and nothing converts."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=(".en.srt",))
    check_listed(env, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    said = {"verdict": "match"}
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, starts=None, line=True, deep=False, streams=None: {
        k: dict(said, why="the heard words match the cues at 5%", audio=0, starts=[]) for k in items})
    row = check_plan(env, tmp_path, mp4)
    assert row["outcome"] == "would_repack", row
    in_the_apply(monkeypatch, lambda: said.update(verdict="mismatch"))
    hook.background(hook.deep_name(mp4), [])
    rec = lines(env, "check_job")[-1]
    assert (rec["outcome"], list(rec["plan_diff"]), env["repacks"]) == ("plan_refused", ["convert_plan"], []), rec
    assert os.path.exists(mp4[:-4] + ".en.srt") and not os.path.exists(tmp_path / ".kept")


def test_the_edit_after_a_remux_keeps_the_rows_edits(env, monkeypatch, tmp_path):
    """Re-reviewer N1 2026-10-22: the one write repairs the header and carries the row's edit, English subtitle track 3
    on. mkvmerge's statistics tags then make track 3 sparse, so the plan of the new file turns track 4 on. That edit is
    the replan's own, so it refuses. Track 3 stays on, as the row says, and the line keeps the repair."""
    from test_write_stage import D, LAVF, header_issue as stage_issue
    env["movies"]["movie/7"]["originalLanguage"] = {"name": "Portuguese"}
    env["probe"] = copy.deepcopy(LAVF)
    after = hook.planned_state(env["probe"], D.decide(env["probe"], "Portuguese")["edits"])[0]
    after["container"]["properties"]["writing_application"] = "mkvmerge v102.0"
    for t, n in zip(after["tracks"][2:], ("50", "1500")):
        t["properties"]["tag_number_of_frames"] = n
    stage_issue(env, monkeypatch, after=after)
    check_listed(env, monkeypatch)
    row = check_plan(env, tmp_path)
    assert row["edits"] == [["track:=3", 1, 0]] and row["header_plan"], row
    hook.background(hook.deep_name(env["path"]), [])
    (rec,) = lines(env, "check_job")
    assert (rec["outcome"], rec["plan_diff"]["edits"], rec["reasons"]) == ("plan_refused", [[["track:=3", 1, 0]], [["track:=4", 1, 0]]], ["header_repaired"]), rec
    flags = {t["properties"]["uid"]: t["properties"]["default_track"] for t in env["files"][env["path"]]["tracks"]}
    assert (flags[3], flags[4], env["mkvpropedit"]) == (True, False, []), flags


def test_a_stop_after_the_jobs_own_conversion_and_edit_goes_on_and_rescans(env, monkeypatch, settings, tmp_path):
    """Re-reviewer N2 2026-10-22: a stop comes after the job converted the file and edited the new one. The job keeps
    the key of the edited file, so its next run goes on from it, and that run sends the rescan the stop held."""
    path, new = mp4_import(env, monkeypatch, sidecars=())
    check_listed(env, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    fake_run, real_edit = hook.subprocess.run, hook.edit

    def run(argv, **kw):   # mkvpropedit gives the file a new mtime, as the real one does
        out = fake_run(argv, **kw)
        if argv[0] == "mkvpropedit":
            st = os.stat(argv[1])
            os.utime(argv[1], ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        return out
    monkeypatch.setattr(hook.subprocess, "run", run)

    def edit(rec, j, edits, apply, *a, **k):   # the stop comes as the edit of the new file ends
        out = real_edit(rec, j, edits, apply, *a, **k)
        hook.STOP.update(asked=apply and out["outcome"] == "edited")
        return out
    monkeypatch.setattr(hook, "edit", edit)
    check_plan(env, tmp_path, path)
    name = hook.deep_name(path)
    assert hook.claim(name)
    hook.background(name, [], claimed=True)
    hook.STOP.update(asked=False, skipped=False)
    job, rescan = hook.job_of(name, True), ("POST", "command", {"name": "RescanMovie", "movieId": 7})
    assert (job["converted"], job["own"]["codes"], job["rescan"]) == (list(hook.file_key(os.stat(new))), ["repacked", "edited"], True), job
    assert rescan not in env["writes"]
    hook.requeue(name)
    hook.background(name, [])
    rec = lines(env, "check_job")[-1]
    assert (rec["outcome"], rec["note"], rescan in env["writes"]) == ("no_change", "this job converted the file before a stop", True), rec


@pytest.mark.parametrize("off", ["subtitles", "audio"])
def test_the_dry_check_plans_the_mute_on_the_flags_its_edits_leave(env, burn, monkeypatch, tmp_path, off):
    """Re-reviewer N3 2026-10-22: English text is burned into a Japanese film. Its English subtitles are off, or its
    English audio plays first. The dry --check plans the import's edit that turns the subtitles or the Japanese audio
    on, so it plans the burned-in subtitle check after that edit, as the job runs. The job makes its edit, and its
    burn-in job turns the English subtitles off and marks the file."""
    japanese_film(env, ("eng", off == "audio", {}))
    if off == "audio":
        env["probe"] = sub_probe(("eng", True, {}), audio=(("jpn", False), ("eng", True)))
    burn["full"] = [verdict("full", True, "eng")]
    check_listed(env, monkeypatch)
    row = check_plan(env, tmp_path)
    assert (row["edits"], row["burn_in"]["edits"]) == ({"subtitles": [["track:=10", 1, 0]], "audio": [["track:=2", 1, 0], ["track:=3", 0, 1]]}[off],
                                                       [["track:=10", 0, 1]]), row
    quick = [r["burned_in"] for r in log_lines(env) if r.get("source") == "check" and (r.get("burned_in") or {}).get("result") == "flagged"]
    assert [b["audio"]["lang"] for b in quick] == ["jpn"], quick   # the quick check hears the audio the edit leaves playing
    hook.background(hook.deep_name(env["path"]), [])
    hook.background(hook.burn_name(env["path"]), [])
    b = [r for r in log_lines(env) if r.get("source") == "burn_in"][-1]
    subs = [t["properties"]["default_track"] for t in env["files"][env["path"]]["tracks"] if t["type"] == "subtitles"]
    assert (b["edit_result"], subs, hook.muted(env["path"])) == ("edited", [False], True), b


@pytest.mark.parametrize("how", ["renamed", "rewritten"])
def test_a_sidecar_that_changed_since_the_dry_run_refuses_the_conversion(env, monkeypatch, settings, tmp_path, how):
    """Re-reviewer N4 2026-10-22: with no hearing the conversion's subtitle check gives no verdict. The digest of the
    conversion holds each sidecar it muxes by name, language, flags, size and mtime. So a sidecar in another language,
    or a new version of the same name, refuses the conversion before any write."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=(".en.srt",))
    check_listed(env, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    check_plan(env, tmp_path, mp4)
    side = mp4[:-4] + (".fr.srt" if how == "renamed" else ".en.srt")
    if how == "renamed":
        os.replace(mp4[:-4] + ".en.srt", side)
    with open(side, "w") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\nBonjour tout le monde\n")
    hook.background(hook.deep_name(mp4), [])
    rec = lines(env, "check_job")[-1]
    assert (rec["outcome"], list(rec["plan_diff"]), env["repacks"], os.path.exists(mp4)) == ("plan_refused", ["convert_plan"], [], True), rec


def test_a_stop_after_the_jobs_own_header_repair_names_it(env, monkeypatch, tmp_path):
    """Re-reviewer N5 2026-10-22: a stop comes right after the job's own first header repair. The next run refuses, as
    no dry run saw the repaired file, and its line says that the job wrote the file. It sends the rescan the stop held."""
    from test_write_stage import header_issue as stage_issue
    stage_issue(env, monkeypatch, first=True)
    real = hook.repack

    def repack(path, j, st, apply, hp=None, edits=()):
        out = real(path, j, st, apply, hp, edits)
        hook.STOP.update(asked=apply)
        return out
    monkeypatch.setattr(hook, "repack", repack)
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    name = hook.deep_name(env["path"])
    assert hook.claim(name)
    hook.background(name, [], claimed=True)
    hook.STOP.update(asked=False, skipped=False)
    hook.requeue(name)
    hook.background(name, [])
    rec = lines(env, "check_job")[-1]
    assert (rec["outcome"], rec["result"]) == ("plan_refused", "refused, this job wrote the file before a stop (header_repaired), so a new dry run "
                                                               "plans the rest"), rec
    assert ("POST", "command", {"name": "RescanMovie", "movieId": 7}) in env["writes"] and env["mkvpropedit"] == []


def test_a_deep_analysis_that_finds_the_jobs_fix_made_never_refuses(env, monkeypatch, settings, tmp_path):
    """Re-reviewer R1 2026-10-22: KEEP_ORIGINALS_DAYS 0, so a mismatched English track that plays loses its flags. The
    dry --check and its deep analysis both turn it off. The job turns it off, so its deep analysis finds nothing to
    write. A field the deep analysis leaves empty writes nothing, so it never refuses."""
    settings(subtitles="deep", keep_days=0)
    env["probe"] = sub_probe(("eng", True, {}), audio=(("eng", True),))
    env["movies"]["movie/7"]["runtime"] = round(talk.DURATION / 60)
    hearing(env, monkeypatch, {"s1": talk.cues(talk.OTHER)})
    check_listed(env, monkeypatch)
    row = check_plan(env, tmp_path)
    assert row["edits"] == row["deep"]["edits"] == [["track:=10", 0, 1]], row
    name = hook.deep_name(env["path"])
    hook.background(name, [])
    hook.background(name, [])
    deep = [r for r in log_lines(env) if r.get("source") == "deep_analysis"][-1]
    assert (deep["outcome"], len(env["mkvpropedit"])) == ("no_change", 1), deep


def test_a_stop_after_the_jobs_own_flag_edit_names_it(env, monkeypatch, tmp_path):
    """Re-reviewer N5 2026-10-22, the same for a flag backfill's job: a stop comes right after its flag edit, which needs
    no rescan. The job keeps the key of the edited file, so the refusal of its next run names the edit."""
    (path,) = listed(env, monkeypatch)
    fake_run, real_edit = hook.subprocess.run, hook.edit

    def run(argv, **kw):   # mkvpropedit gives the file a new mtime, as the real one does
        out = fake_run(argv, **kw)
        if argv[0] == "mkvpropedit":
            st = os.stat(argv[1])
            os.utime(argv[1], ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        return out
    monkeypatch.setattr(hook.subprocess, "run", run)

    def edit(rec, j, edits, apply, *a, **k):
        out = real_edit(rec, j, edits, apply, *a, **k)
        hook.STOP.update(asked=apply and out["outcome"] == "edited")
        return out
    monkeypatch.setattr(hook, "edit", edit)
    plans, _ = dry_run(tmp_path)
    queue(plans)
    name = hook.deep_name(path)
    assert hook.claim(name)
    hook.background(name, [], claimed=True)
    hook.STOP.update(asked=False, skipped=False)
    assert hook.job_of(name, True)["own"] == {"key": list(hook.file_key(os.stat(path))), "codes": ["edited"]}
    hook.requeue(name)
    hook.background(name, [])
    rec = lines(env)[-1]
    assert (rec["outcome"], rec["result"], len(env["mkvpropedit"])) == (
        "plan_refused", "refused, this job wrote the file before a stop (edited), so a new dry run plans the rest", 1), rec


def test_a_sidecar_move_after_the_jobs_own_conversion_runs(env, monkeypatch, settings, tmp_path):
    """Reviewer X4 2026-10-22: after the job's own conversion the rest runs as --check --apply runs it. A subtitle tool
    puts a sidecar with the lines of another film beside the new file as the conversion ends. The check finds the
    mismatch, and sidecar_fix() moves the sidecar to the kept originals."""
    from test_arr_media_guard import srt_text
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    check_listed(env, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    env["mkv_probe"] = sub_probe(audio=(("eng", True),))
    env["mkv_probe"]["container"]["type"] = "Matroska"
    env["movies"]["movie/7"]["runtime"] = round(talk.DURATION / 60)
    hearing(env, monkeypatch, {})
    real = hook.convert

    def side(path):
        with open(path, "w") as f:
            f.write(srt_text(talk.cues(talk.OTHER)))

    def convert(*a, **k):
        out = real(*a, **k)
        if out[0] == "repacked":
            outside(lambda: side(out[3][:-4] + ".en.srt"))
        return out
    monkeypatch.setattr(hook, "convert", convert)
    check_plan(env, tmp_path, mp4)
    hook.background(hook.deep_name(mp4), [])
    rec = lines(env, "check_job")[-1]
    assert rec["outcome"] not in ("plan_refused", "error") and [e["result"] for e in rec.get("sidecars") or []] == ["moved"], rec
    assert not os.path.exists(mkv[:-4] + ".en.srt")


def test_a_blocked_remux_whose_fit_moved_in_the_apply_refuses(env, monkeypatch, tmp_path):
    """Reviewer G10 2026-10-22: the English track of an English film is 2 s late in the dry runs and 4 s late in the
    apply. Bytes past the Segment end block the remux. act() compares all that the run will write before its first
    write, so the job refuses and writes nothing."""
    from test_write_stage import JOINED, PADDED, header_issue as stage_issue
    late_english_film(env, monkeypatch)
    stage_issue(env, monkeypatch, **dict(PADDED, edit_blocked=[JOINED]))
    check_listed(env, monkeypatch)
    in_the_apply(monkeypatch, lambda: hearing(env, monkeypatch, {"s1": talk.cues(talk.RIGHT, offset=4.0)}))
    check_plan(env, tmp_path)
    hook.background(hook.deep_name(env["path"]), [])
    rec = lines(env, "check_job")[-1]
    assert (rec["outcome"], list(rec["plan_diff"]), env["mkvpropedit"]) == ("plan_refused", ["subtitles"], []), rec
