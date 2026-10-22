# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The write gate of a queued plan, see gate.py. Each write of a job that applies a plan row goes through gate.allow(),
and the guard, an audit hook, fails a write on the job's file or a sidecar that no allow() let through. The flow tests
run each kind of write in a queued job twice: through the gate, and with the gate's window shut for that kind, as a
write that skips the gate would run."""
import copy
import os
import shutil
import subprocess

import pytest

from test_arr_media_guard import (burn, check_listed, env, hearing, hook, japanese_film, log_lines, mp4_import, one_alert_film, radarr_extras,  # noqa: F401
                                  sub_probe, talk, verdict)
from test_queued_plans import check_plan, outside
from test_write_stage import header_issue

CTX = type("Ctx", (), {"row": None, "apply": True, "background": False})   # a run with no plan row


def films(tmp_path):
    """A film, its sidecar and another film in one folder, and a file outside it. Returns their paths."""
    (tmp_path / "films").mkdir()
    paths = [str(tmp_path / "films" / n) for n in ("Film A.mkv", "Film A.en.srt", "Film B.mkv")] + [str(tmp_path / "new")]
    for p in paths:
        open(p, "wb").write(b"x")
    return paths


WRITES = {"open": lambda p: open(p, "ab").close(), "os.open": lambda p: os.close(os.open(p, os.O_WRONLY)),
          "replace onto": lambda p: os.replace(os.path.join(os.path.dirname(os.path.dirname(p)), "new"), p),
          "rename away": lambda p: os.rename(p, p + ".old"), "remove": os.remove, "link": lambda p: os.link(p, p[:-4] + ".fr.srt"),
          "symlink": lambda p: os.symlink(p, p[:-4] + ".fr.srt"), "truncate": lambda p: os.truncate(p, 0),
          "shutil.move": lambda p: shutil.move(p, p + ".old"), "shutil.copyfile": lambda p: shutil.copyfile(__file__, p),
          "mkvpropedit": lambda p: subprocess.run(["mkvpropedit", p, "--edit", "track:1"]),
          "mkvmerge -o": lambda p: subprocess.run(["ionice", "-c3", "nice", "-n", "19", "mkvmerge", "-q", "-o", p, __file__]),
          "mkvextract": lambda p: subprocess.run(["mkvextract", __file__, "tracks", f"2:{p}"]),
          "ffmpeg": lambda p: subprocess.run(["ffmpeg", "-nostdin", "-i", __file__, "-c", "copy", p])}


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("how", list(WRITES))
def test_the_guard_fails_each_kind_of_write_outside_the_gate(tmp_path, how, which):
    """Each write on the film or its sidecar raises Escaped before it runs, and the guard raises it again as the job
    ends. The folder stays as it was."""
    paths = films(tmp_path)
    with pytest.raises(hook.Escaped):
        with hook.armed(paths[0]):
            WRITES[how](paths[which])
    assert sorted(os.listdir(tmp_path / "films")) == ["Film A.en.srt", "Film A.mkv", "Film B.mkv"]
    assert [open(p, "rb").read() for p in paths] == [b"x"] * 4 and hook.STATE["folder"] is None


def test_the_gate_lets_its_own_writes_through_and_the_guard_keeps_a_caught_one(tmp_path):
    """A read, a write of another film and a write inside allow() pass. A write that its caller catches still fails the
    job, as the guard raises it again at its end."""
    film, side, other, _ = films(tmp_path)
    with pytest.raises(hook.Escaped):
        with hook.armed(film):
            open(film, "rb").close()
            open(other, "ab").close()
            hook.allow(CTX, "flag edit", lambda: open(film, "ab").close())
            assert hook.STATE["open"] == []
            try:
                os.remove(side)
            except AssertionError:   # a caller that catches it
                pass
    assert os.path.exists(side)
    os.remove(side)   # the guard is off again


def test_a_late_compare_opens_only_inside_its_allow(tmp_path):
    """A conversion compares its digest inside its allow(), see gate.opened(). A digest that differs refuses. The same
    compare outside an allow() lets nothing through."""
    film = films(tmp_path)[0]
    ctx = type("Ctx", (), {"row": {"convert_plan": "a"}, "apply": True, "background": False})
    with pytest.raises(hook.Escaped):
        with hook.armed(film):
            hook.allow(ctx, "conversion", lambda: (hook.opened(ctx, "conversion", convert_plan="a"), open(film, "ab").close()), late=True)
            with pytest.raises(hook.PlanRefused):
                hook.allow(ctx, "conversion", lambda: hook.opened(ctx, "conversion", convert_plan="b"), late=True)
            hook.opened(ctx, "conversion", convert_plan="a")
            open(film, "ab").close()


def touched(monkeypatch, name):
    """Make the stand-in of hook.<name>, a remux, write the file as a remux does: a new file renamed over it."""
    fake = getattr(hook, name)

    def remux(path, j, st, apply, *a, **k):
        out = fake(path, j, st, apply, *a, **k)
        if apply and out[0] in ("subtitles_remuxed", *hook.REPAIRED):
            with open(path + ".new", "wb") as f:
                f.write(open(path, "rb").read() + b"r")
            os.replace(path + ".new", path)
        return out
    monkeypatch.setattr(hook, name, remux)


def shut(monkeypatch, kind):
    """Shut the gate's window for the writes of kind, as a write that skips the gate runs."""
    real = hook.let
    monkeypatch.setattr(hook, "let", lambda k: None if k == kind else real(k))


def run_job(env, path):
    """Run the queued job of path and return its decision line."""
    hook.background(hook.deep_name(path), [])
    return [r for r in log_lines(env) if r.get("job") == hook.deep_name(path) and "outcome" in r][-1]


def conversion(env, monkeypatch, settings, tmp_path):
    mp4, _ = mp4_import(env, monkeypatch, sidecars=(".en.srt",))
    check_listed(env, monkeypatch)
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    check_plan(env, tmp_path, mp4)
    return mp4, "repacked"


def first_repair(env, monkeypatch, settings, tmp_path):
    header_issue(env, monkeypatch, first=True)
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    return env["path"], "header_repaired"


def one_write_repair(env, monkeypatch, settings, tmp_path):
    header_issue(env, monkeypatch)
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    return env["path"], "header_repaired"


def flag_edit(env, monkeypatch, settings, tmp_path):
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    return env["path"], "edited"


def replan_edit(env, monkeypatch, settings, tmp_path):
    """The remux of the one write leaves the flags as they were, so the replan after it makes the row's edits."""
    for i, t in enumerate(env["probe"]["tracks"]):
        t["id"] = i
    header_issue(env, monkeypatch, after=copy.deepcopy(env["probe"]))
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    return env["path"], "edited"


def subtitle_remux(env, monkeypatch, settings, tmp_path):
    from test_arr_media_guard import late_english_film
    late_english_film(env, monkeypatch)
    touched(monkeypatch, "resub")
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    return env["path"], "subtitle_retimed"


def failed_remux(env, monkeypatch, settings, tmp_path):
    """Portuguese audio plays first in an English film, and its English subtitles are 2 s late. The subtitle remux that
    would carry the flag edits fails, so mkvpropedit makes them."""
    from test_arr_media_guard import late_english_film
    late_english_film(env, monkeypatch)
    env["probe"] = sub_probe(("eng", False, {}), audio=(("por", True), ("eng", False)))
    env["resub_fails"] = True
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    return env["path"], "edited"


def sidecars(env, monkeypatch, settings, tmp_path):
    settings(keep_days=7)   # a retime keeps the original sidecar
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    env["probe"] = sub_probe(audio=(("eng", True),))
    env["movies"]["movie/7"]["runtime"] = round(talk.DURATION / 60)
    hearing(env, monkeypatch, {})
    ms = lambda t: f"{int(t // 3600):02}:{int(t % 3600 // 60):02}:{int(t % 60):02},{int(round(t * 1000)) % 1000:03}"
    with open(env["path"][:-4] + ".en.srt", "w") as f:
        f.write("".join(f"{i}\n{ms(s)} --> {ms(e)}\n{t}\n\n" for i, (s, e, t) in enumerate(talk.cues(talk.RIGHT, offset=2.0), 1)))
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    return env["path"], "sidecars_changed"


def hidden_extras(env, monkeypatch):
    """An MP4 with a Spanish .srt, a .pdf and an .nfo that Radarr tracks, see radarr_extras(). Its conversion hides the
    .srt and the .pdf until Radarr drops the old record. Radarr is another program, so the guard sees none of its
    writes. Returns (mp4, mkv, the names of the hidden extras)."""
    mp4, mkv, names = radarr_extras(env, monkeypatch)[:3]
    app = env["on_write"]
    env["on_write"] = lambda *a: outside(lambda: app(*a))
    check_listed(env, monkeypatch)
    return mp4, mkv, names


def extras(env, monkeypatch, settings, tmp_path):
    mp4 = hidden_extras(env, monkeypatch)[0]
    check_plan(env, tmp_path, mp4)
    return mp4, "repacked"


KINDS = {"conversion": ("conversion", conversion), "first header repair": ("header repair", first_repair),
         "header repair of the one write": ("header repair", one_write_repair), "flag edit": ("flag edit", flag_edit),
         "flag edit after a failed remux": ("flag edit", failed_remux), "replan edit": ("replan edit", replan_edit), "subtitle remux": ("subtitle remux", subtitle_remux), "sidecars": ("sidecars", sidecars),
         "extras of the conversion": ("extras", extras)}


@pytest.mark.parametrize("skips", [False, True])
@pytest.mark.parametrize("case", list(KINDS))
def test_each_write_of_a_queued_job_runs_only_through_the_gate(env, monkeypatch, settings, tmp_path, case, skips):
    """Through the gate the queued --check job makes its write. A write of the same kind that skips the gate fails the
    job before it runs: the guard raises Escaped, and the line is an error."""
    kind, setup = KINDS[case]
    path, wrote = setup(env, monkeypatch, settings, tmp_path)
    if skips:
        shut(monkeypatch, kind)
    rec = run_job(env, path)
    if skips:
        assert rec["outcome"] == "error" and "Escaped" in rec["result"] and kind.split()[0] not in str(rec.get("reasons")), rec
    else:
        assert rec["outcome"] != "error" and wrote in [rec["outcome"], *rec.get("reasons", [])] + ((rec.get("subremux") or {}).get("codes") or []), rec


@pytest.mark.parametrize("skips", [False, True])
def test_the_burn_in_mute_of_a_queued_check_runs_only_through_the_gate(env, burn, monkeypatch, tmp_path, skips):
    japanese_film(env, ("eng", True, {}))
    burn["full"] = [verdict("full", True, "eng")]
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    hook.background(hook.deep_name(env["path"]), [])
    if skips:
        shut(monkeypatch, "burn-in mute")
    edits = len(env["mkvpropedit"])
    hook.background(hook.burn_name(env["path"]), [])
    b = [r for r in log_lines(env) if r.get("source") == "burn_in"][-1]
    if skips:
        assert (b["outcome"], "Escaped" in b["result"], len(env["mkvpropedit"])) == ("error", True, edits), b
    else:
        assert (b["edit_result"], len(env["mkvpropedit"])) == ("edited", edits + 1), b


@pytest.mark.parametrize("skips", [False, True])
def test_the_deep_analysis_of_a_queued_check_writes_only_through_the_gate(env, monkeypatch, settings, tmp_path, skips):
    one_alert_film(env, monkeypatch, settings, "fixed")
    env["probe"] = sub_probe(("eng", True, {}), audio=(("eng", True),))
    touched(monkeypatch, "resub")
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path)
    name = hook.deep_name(env["path"])
    hook.background(name, [])
    if skips:
        shut(monkeypatch, "subtitle remux")
    hook.background(name, [])
    deep = [r for r in log_lines(env) if r.get("source") == "deep_analysis"][-1]
    if skips:
        assert deep["outcome"] == "error" and "Escaped" in deep["result"], deep
    else:
        assert (deep["outcome"], deep["subremux"]["done"]) == ("subtitles_remuxed", True), deep


@pytest.mark.parametrize("queued", [False, True])
def test_the_extras_of_the_jobs_own_conversion_come_back(env, monkeypatch, tmp_path, queued):
    """Reviewer S1 2026-10-22: after its own conversion, a queued --check job moves the hidden extras back through the
    gate, as --check --apply does, see process.after_edit(). The extras are beside the new file, and no hidden folder
    or pending entry stays."""
    mp4, mkv, names = hidden_extras(env, monkeypatch)
    if queued:
        check_plan(env, tmp_path, mp4)
        rec = run_job(env, mp4)
    else:
        hook.main(["--check", mp4, "--apply"])
        rec = [r for r in log_lines(env) if r.get("source") == "check" and "outcome" in r][-1]
    folder = os.path.dirname(mkv)
    assert rec["outcome"] == "edited" and rec["repack"]["settle"]["left"] == [], rec
    assert set(names) <= set(os.listdir(folder)) and not os.path.isdir(os.path.join(folder, hook.CFG.hide_dir)) and hook.pending_edit() == {}


def test_a_queued_check_leaves_the_extras_of_another_runs_conversion_hidden(env, monkeypatch, tmp_path):
    """Reviewer S1 2026-10-22, second shape: an import converted the MP4, and its rescan left the old record, so the
    extras wait hidden with a pending entry. The plan of a queued --check of the new file holds no move of them, so the
    job leaves them for the next import or convert.pending_recover()."""
    mp4, mkv, names, _, _ = radarr_extras(env, monkeypatch)
    real = env["on_write"]
    env["on_write"] = lambda a, p, method, body: None if p == "command" and body["name"] == "RescanMovie" else real(a, p, method, body)
    hook.main([])
    (key,) = hook.pending_edit()
    env["on_write"] = lambda *a: outside(lambda: real(*a))   # Radarr drops the old record from now on
    hook.pending_edit(key, dict(hook.pending_edit()[key], pid=999999))   # the import's process ended
    monkeypatch.setattr(hook, "job_alive_pid", lambda pid, start=None: False)
    check_listed(env, monkeypatch)
    check_plan(env, tmp_path, mkv)
    rec = run_job(env, mkv)
    assert (rec["outcome"], "settle" in rec, list(hook.pending_edit())) == ("no_change", False, [key]), rec
    assert sorted(os.listdir(os.path.join(os.path.dirname(mkv), hook.CFG.hide_dir))) == sorted(names)


@pytest.mark.parametrize("late", [False, True])
def test_a_dry_pass_opens_no_window(tmp_path, late):
    """Reviewer G5 2026-10-22: a dry pass writes nothing, so its allow() and its late compare let no write through."""
    film = films(tmp_path)[0]
    ctx = type("Ctx", (), {"row": {"convert_plan": "a"}, "apply": False, "background": False})
    write = (lambda: (hook.opened(ctx, "conversion", convert_plan="a"), open(film, "ab").close())) if late else lambda: open(film, "ab").close()
    with pytest.raises(hook.Escaped):
        with hook.armed(film):
            hook.allow(ctx, "conversion", write, late=late)


def test_a_write_in_the_dry_pass_of_a_queued_job_fails_the_job(env, monkeypatch, tmp_path):
    """Reviewer G5 2026-10-22: edit() opens the film for write in the dry pass of a queued --check job, as a write site
    that ignores apply False would. The guard fails the job."""
    check_listed(env, monkeypatch)
    real, dry = hook.edit, []

    def edit(rec, j, edits, apply, *a, **k):
        if not apply and edits and hook.STATE["folder"]:   # the dry pass of the queued job
            dry.append(rec["path"])
            open(rec["path"], "ab").close()   # an open for write that writes no byte
        return real(rec, j, edits, apply, *a, **k)
    monkeypatch.setattr(hook, "edit", edit)
    check_plan(env, tmp_path)
    rec = run_job(env, env["path"])
    assert (dry, rec["outcome"], "Escaped" in rec["result"]) == ([env["path"]], "error", True), rec
