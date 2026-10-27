# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The commentary flag (docs/design.md, "Language tags"). An audio or subtitle track whose title reads as commentary
and that lacks FlagCommentary gets it, in the header edit that sets the Original language flag, see decide.retag().
Every file is made up."""
import shutil
import subprocess

import pytest

from test_arr_media_guard import REAL_MKVMERGE, decided, env, hook, mk  # noqa: F401

FLAG = "flag-commentary"
# The titles that negate commentary, see decide.NO_COMMENT_TITLE. test_arr_decide.py gives each the role full or main.
NEGATED = ("No Commentary", "Commentary-free", "Commentary free", "Without Commentary", "w/o Commentary", "Non-Commentary",
           "English - No Commentary", "English [No Commentary]")


def film(*more):
    """An English film: English audio, then the tracks more as (type, title, more properties)."""
    ts = [mk("video", "und", 1, True, pixel_dimensions="1920x1080"), mk("audio", "eng", 2, True, audio_channels=6)]
    ts += [mk(typ, "eng", 3 + k, False, **({"track_name": title} if title else {}), **({"audio_channels": 2} if typ == "audio" else {}), **kw)
           for k, (typ, title, kw) in enumerate(more)]
    return {"container": {"properties": {"duration": 7200 * 10**9}}, "tracks": ts}


@pytest.mark.parametrize("kind", ["audio", "subtitles"])
@pytest.mark.parametrize("title, flag, edit", [
    ("Commentary", None, [1, None]), ("Director's Comm", None, [1, None]), ("English (cmt)", None, [1, None]),   # each title word
    ("Comments by the Cast", False, [1, False]),   # a flag of 0 goes to 1, and its undo sets 0 again
    ("Commentary", True, None),                    # a flag set already stays
    ("Production Notes", None, None), ("Trivia", None, None), ("English", None, None),   # a notes title, and no commentary at all
    ("Text Commentary", None, [1, None]), ("Commentary (no subtitles)", None, [1, None]),   # a "no" or "free" on another word
    ("Non-English Commentary", None, [1, None]), ("Commentary (spoiler-free)", None, [1, None]),
    *[(t, None, None) for t in NEGATED],   # a title that negates commentary holds the dialogue
])
def test_a_title_that_reads_as_commentary_sets_the_flag(kind, title, flag, edit):
    j = film((kind, title, {} if flag is None else {"flag_commentary": flag}))
    got = [e for e in hook.arr_decide.retag(j, table=hook.langs())["edits"] if hook.arr_decide.prop(e) == FLAG]
    assert got == ([["track:=3", *edit, FLAG]] if edit else []), got


def test_an_import_sets_the_flag_and_says_so(env, monkeypatch, settings):
    """The flag goes on in the header edit, with no remux. The decision line names the rule and the note, the edit
    verifies, and with DISCORD_POSTS all the change post says it. A track with the flag keeps it."""
    settings(discord_posts="all")
    env["probe"] = film(("audio", "Commentary", {}), ("audio", "Director's Commentary", {"flag_commentary": True}), ("subtitles", "Commentary", {}),
                        ("subtitles", "Production Notes", {}))
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited" and [e for e in rec["edits"] if hook.arr_decide.prop(e) == FLAG] == [
        ["track:=3", 1, None, FLAG], ["track:=5", 1, None, FLAG]], rec["edits"]
    assert env["repacks"] == [] and [a for args in env["mkvpropedit"] for a in args if a.startswith(FLAG)] == [f"{FLAG}=1"] * 2, env["mkvpropedit"]
    assert "commentary flag set" in rec["edit_rules"] and 'a2: commentary flag None -> 1, the title "Commentary" reads as commentary' in rec["notes"], rec
    posts = [b["embeds"][0] for m, u, b in env["http"] if m == "POST" and "embeds" in b]
    assert [p["description"] for p in posts if p["title"] == "Commentary flags changed"] == [
        "Turned the commentary flag on for the **English commentary audio (track 2)** and the **English commentary subtitles (track 1)**."], posts


def test_a_dry_run_plans_the_flag_and_edits_nothing(env, monkeypatch, capsys):
    """--check with no --apply plans the edit and prints it, and nothing runs mkvpropedit."""
    from test_arr_media_guard import check_listed, checked
    env["probe"] = film(("audio", "Commentary", {}))
    check_listed(env, monkeypatch)
    hook.main(["--check", env["path"]])
    (rec,) = checked(env)
    assert rec["outcome"] == "dry_run" and ["track:=3", 1, None, FLAG] in rec["edits"] and env["mkvpropedit"] == [], rec
    assert "commentary flag" in capsys.readouterr().out


@pytest.mark.skipif(not (shutil.which("ffmpeg") and REAL_MKVMERGE), reason="needs ffmpeg and mkvmerge")
def test_mkvpropedit_sets_the_flag_and_the_undo_deletes_it(tmp_path):
    """The real tools: the edit's mkvpropedit arguments set FlagCommentary, unapplied() reads it back, and the undo of
    a missing flag deletes it again. A remux that carries the edit sets it too, see remux.planned_state()."""
    a, mkv = tmp_path / "a.mka", tmp_path / "Film A.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-c:a", "flac", str(a)], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(mkv), "--track-name", "0:Commentary", str(a)], check=True)
    j = hook.mkvmerge(str(mkv))
    edits = [e for e in hook.arr_decide.retag(j, table=hook.langs())["edits"] if hook.arr_decide.prop(e) == FLAG]
    assert len(edits) == 1 and hook.arr_decide.unapplied(j, edits) == edits
    subprocess.run(["mkvpropedit", str(mkv)] + hook.propedit_args(edits, 1), check=True, capture_output=True)
    after = hook.mkvmerge(str(mkv))
    assert after["tracks"][0]["properties"].get("flag_commentary") is True and hook.arr_decide.unapplied(after, edits) == []
    assert hook.needed(after, edits) == [] and hook.needed(j, edits) == edits   # an edit the file shows already never runs
    subprocess.run(["mkvpropedit", str(mkv)] + hook.propedit_args(edits, 2), check=True, capture_output=True)
    assert "flag_commentary" not in hook.mkvmerge(str(mkv))["tracks"][0]["properties"]
    planned = hook.planned_state(j, edits)[0]
    assert planned["tracks"][0]["properties"]["flag_commentary"] is True, planned
