# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""A SubRip cue that ends at its start is no cue (docs/design.md, "Conversion"). mkvmerge drops it, and no player shows
it, so a conversion muxes its sidecar. A cue that ends before it starts shows up to the next cue in a player, so its
sidecar stays beside the new file, see proof.misread(). Every file and every line is made up."""
import shutil
import subprocess

import pytest

from test_arr_media_guard import convertible, decided, env, hook, mp4_import, proved  # noqa: F401

HEAD = "1\n00:00:01,000 --> 00:00:02,000\nGood evening.\n\n2\n00:00:03,000 --> 00:00:04,000\nNice to meet you.\n\n"
ZERO = "3\n00:00:05,000 --> 00:00:05,000\nYour future, your life.\n\n4\n00:00:06,000 --> 00:00:07,000\nYou put her first.\n"
BACK = "3\n00:00:05,000 --> 00:00:04,500\nYour future, your life.\n\n4\n00:00:06,000 --> 00:00:07,000\nYou put her first.\n"
BOTH = ZERO + "\n5\n00:00:08,000 --> 00:00:07,500\nBack again.\n"


@pytest.mark.parametrize("tail, why", [(ZERO, None), (BACK, "mkvmerge drops 1 of its cues"), (BOTH, "mkvmerge drops 1 of its cues")])
def test_misread_counts_a_cue_that_ends_at_its_start_as_none(tail, why):
    assert hook.misread(*hook.arr_decide.srt_read(HEAD + tail)) == why


@pytest.mark.parametrize("tail, fault, count", [
    (ZERO, None, 3),   # the cue of length 0 is none on either side
    (BACK, "the sidecar Movie (2020).en.srt holds 3 cues in the new file, 4 in the original", 4),
])
def test_the_sidecar_proof_counts_a_cue_that_ends_at_its_start_as_none(convertible, tmp_path, tail, fault, count):
    """The real conversion of a made-up MP4 with the sidecar, and its proof. mkvmerge 102 drops both shapes. The cue
    that ends at its start passes, and the proof refuses the one that ends before it starts."""
    shutil.copy(convertible / "Movie (2020).mp4", tmp_path / "Movie (2020).mp4")
    (tmp_path / "Movie (2020).en.srt").write_text(HEAD + tail)
    mp4, out = str(tmp_path / "Movie (2020).mp4"), tmp_path / "new.mkv"
    subs = hook.sidecar_subs(mp4)
    r = subprocess.run(hook.convert_cmd(mp4, str(out), hook.mkvmerge(mp4), subs, None)[5:], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    got, proof = proved(mp4, str(out), subs, str(tmp_path))
    assert (got, proof[-1]["count"]) == (fault, count), (got, proof)
    track = [t for t in hook.mkvmerge(str(out))["tracks"] if t["type"] == "subtitles"][-1]
    assert int(track["properties"]["tag_number_of_frames"]) == 3, track   # the cues mkvmerge wrote


@pytest.mark.parametrize("tail, left", [(ZERO, False), (BACK, True)])
def test_a_conversion_muxes_a_sidecar_whose_dropped_cue_no_player_shows(env, monkeypatch, tail, left):
    """An import of an MP4 converts it. A sidecar whose only dropped cue ends at its start goes into the MKV, so no
    notice names it. One whose cue ends before it starts stays beside the file, and the notice says why."""
    mp4, mkv = mp4_import(env, monkeypatch, sidecars=())
    side = mkv[:-4] + ".en.srt"
    with open(side, "w") as f:
        f.write(HEAD + tail)
    hook.main([])
    rec = decided(env)
    assert rec["outcome"] == "edited", rec
    assert rec["repack"].get("sidecars_unread") == ({"Film A (1979) WEBDL-1080p.en.srt": "mkvmerge drops 1 of its cues"} if left else None), rec["repack"]
    assert any("can't go into the MKV" in a for a in rec.get("alerts") or []) == left, rec.get("alerts")
