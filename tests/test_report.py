# arr-media-guard, a Sonarr and Radarr import hook that sets default tracks and catches broken files.
# Copyright (C) 2026 samwiseg0
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""The golden texts of report.py, the words of every decision output. Each template of a finding, an action, a
subtitle sentence and a dry-run remux has its text here, in each tense it has, and each target has its line. The other
test files assert codes and fields, so a reworded template changes this file only.

Run: pytest tests/test_report.py
"""
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

import amg

os.environ["ARR_MEDIA_GUARD_ENV"] = "/nonexistent/arr-media-guard.env"
h = amg.load("arr_media_guard_report")
h.CFG = dataclasses.replace(h.CFG, instance="host1", name="arr-media-guard")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SILENT = "all 3 audio samples are digital silence"
RESTORED = {"name": "Radarr", "came": ["Film A (1979) HDTV-720p.mp4"], "linked": True, "own_copy": False, "others": 0, "stayed": None}
NOTHING_BACK = {"name": "Radarr", "came": [], "linked": True, "own_copy": True, "others": 0, "stayed": "the recycle bin copy changed since its check"}

# (finding, alert title, color, text in the done tense)
FINDINGS = [
    ({"kind": "language", "want": "English", "has": ["por"]}, "Wrong language", "amber", "No audio track is English. The file has por."),
    ({"kind": "language", "want": "English or Spanish", "has": ["por", "fre"]}, "Wrong language", "amber",
     "No audio track is English or Spanish. The file has por, fre."),
    ({"kind": "runtime", "runs": "10:05", "listed": 62}, "Wrong runtime", "amber", "It runs 10:05, but the listed runtime is 62 minutes."),
    ({"kind": "duration", "why": "The container says 3:05:08, but the streams run 2:03:26."}, "Broken duration header", "amber",
     "The container says 3:05:08, but the streams run 2:03:26."),
    ({"kind": "episode", "why": "the release's NFO says Squidtastic Voyage/That's No Lady, which Sonarr lists as S04E23, S04E27. It was imported "
                                "as S04E15", "names": "S04E23 and S04E27"}, "Wrong episode", "amber",
     "The release's NFO says Squidtastic Voyage/That's No Lady, which Sonarr lists as S04E23, S04E27. It was imported as S04E15. Check the "
     "series' episode order in Sonarr, or import the file to S04E23 and S04E27 by hand."),
    ({"kind": "content", "signals": ["the audio is por, the item's languages are eng", "the release name's year is 2017, the item's 1979"],
      "points": 2}, "Wrong content", "amber",
     "The audio is por, the item's languages are eng. The release name's year is 2017, the item's 1979. That is 2 points, and a re-grab needs 2."),
    ({"kind": "audio", "doubts": ["1 of 3 audio samples are digital silence", "the late sample is empty"]}, "Audio check uncertain", "amber",
     "1 of 3 audio samples are digital silence. The late sample is empty."),
    ({"kind": "video", "doubts": ["1 of 3 video windows are bad"]}, "Video check uncertain", "amber", "1 of 3 video windows are bad."),
    ({"kind": "audio", "certain": SILENT, "action": {"code": "dry_run"}}, "Broken audio", "red", "All 3 audio samples are digital silence."),
    ({"kind": "video", "certain": "3 of 3 video windows are bad", "action": {"code": "unconfirmed"}}, "Corrupt video, not confirmed", "amber",
     "3 of 3 video windows are bad."),
    ({"kind": "damage", "fault": "the audio has invalid data", "line": "mkvmerge: invalid data.", "refusal": "the packets differ",
      "action": {"code": "capped", "cap": 10}}, "Damaged source", "red",
     "The conversion found a damaged source, because the audio has invalid data. mkvmerge: invalid data. The proof refused the new file, because "
     "the packets differ."),
    ({"kind": "damage", "fault": "the video has gaps", "line": "ffmpeg: gap", "refusal": None, "action": {"code": "would_regrab", "kind": "damage"}},
     "Damaged source, would re-grab", "red", "The conversion found a damaged source, because the video has gaps. ffmpeg: gap."),
    ({"kind": "repack", "container": "MP4/QuickTime", "why": "mkvmerge exited 2: x"}, "Repack failed", "amber",
     "The repack into Matroska failed, so the original MP4/QuickTime file stays. Mkvmerge exited 2: x."),
    ({"kind": "repack", "state": "converted", "note": "a conversion is done, and its 2 extras wait in .hide"}, "Stopped conversion", "amber",
     "A conversion is done, and its 2 extras wait in .hide."),
    ({"kind": "repack", "state": "remux", "note": "a conversion stopped in state remux"}, "Stopped conversion", "amber",
     "A conversion stopped in state remux. Check these files by hand."),
    ({"kind": "header", "why": "the size changed from 1000 to 2000 bytes"}, "Header repair failed", "amber",
     "The header repair failed, so the file stays as it was. The size changed from 1000 to 2000 bytes."),
    ({"kind": "cut", "why": "no runtime is listed, so a cut file cannot be told from a runaway subtitle"}, "File may be cut", "amber",
     "A subtitle runs far past the video and the audio, but no runtime is listed, so a cut file cannot be told from a runaway subtitle. The file "
     "may be cut, or the subtitle may belong to another episode or cut. It stays as it is, subtitles included. Check whether the video ends on "
     "the credits."),
    ({"kind": "subtitle", "issue": ["a subtitle event runs to 13:23:33, past the video and the audio at 1:54:33"], "tracks": ["7 (S_HDMV/PGS)"]},
     "Subtitle runs past the end", "amber",
     "A subtitle event runs to 13:23:33, past the video and the audio at 1:54:33. Subtitle track 7 (S_HDMV/PGS) is not SubRip, so the hook cannot cut it."),
    ({"kind": "sublang", "mismatch": ["s1 is tagged eng, but its text reads as rum"], "muted": ["s1 loses its default and forced flags, x"]},
     "Subtitle language", "amber",
     "S1 is tagged eng, but its text reads as rum. Nothing else backs a new tag, so the tag stays. S1 loses its default and forced flags, x. Check "
     "the track and fix its tag."),
    ({"kind": "sublang", "mismatch": ["s1 is tagged eng, but its text reads as rum"], "muted": []}, "Subtitle language", "amber",
     "S1 is tagged eng, but its text reads as rum. Nothing else backs a new tag, so the tag stays. Check the track and fix its tag."),
    ({"kind": "edit", "error": "VERIFY FAILED, flags did not change", "unread": None, "on": ["a2 eng"]}, "Flag edit failed", "amber",
     "The flag edit failed. VERIFY FAILED, flags did not change The file still reads, and its default tracks are a2 eng."),
    ({"kind": "edit", "error": "mkvpropedit failed: x", "unread": "RuntimeError: mkvmerge: not a Matroska file", "on": None}, "Flag edit failed",
     "amber", "The flag edit failed. mkvpropedit failed: x The file no longer reads: RuntimeError: mkvmerge: not a Matroska file"),
    ({"kind": "edit", "error": "mkvpropedit failed: x", "unread": None, "on": None}, "Flag edit failed", "amber", "The flag edit failed. mkvpropedit failed: x "),
    ({"kind": "edit", "error": "VERIFY FAILED, flags did not change", "unread": None, "on": []}, "Flag edit failed", "amber",
     "The flag edit failed. VERIFY FAILED, flags did not change The file still reads, and its default tracks are none."),
    ({"kind": "policy", "file": "/etc/arr-media-guard/policy.json", "error": "line 3: a comma is missing",
      "action": {"code": "no_policy", "file": "Film A.mkv"}}, "Policy did not load", "amber",
     "/etc/arr-media-guard/policy.json did not load, so the hook edits nothing. line 3: a comma is missing"),
    ({"kind": "submatch", "lines": [{"code": "removed", "track": "s1", "why": "the words differ", "by": "hook", "kept": "/k/Film.mkv"}]},
     "Wrong subtitle", "amber", "Subtitle track s1 does not match the audio, the words differ. The hook removed it, and the original file is kept at "
                                "/k/Film.mkv."),
    ({"kind": "subtiming", "lines": [{"code": "check_flash", "track": "s1", "median": 0.25}, {"code": "sweep", "far": [["s1", 3600, 1.5]]}]},
     "Subtitle timing", "amber", "Subtitle s1 flashes its cues: its median cue shows 0.25 s. SUBTITLES is check, so its ends stay. The sweep heard "
                                 "parts of the file off the fitted line: s1 at 1:00:00 by +1.50 s. The times stay as the check decided."),
]

# (action, the end it gives the title of broken audio, its text). Each text ends with the restore, see restored().
ACTIONS = [
    ({"code": "regrabbed", "name": "Radarr", "kind": "audio", "n": 1}, ", re-grabbed",
     "The hook deleted the file, re-monitored it and marked the grab failed, so Radarr searches again."),
    (dict(RESTORED, code="regrabbed", kind="audio", n=1), ", old file restored",
     "The hook put back the old file from the recycle bin: Film A (1979) HDTV-720p.mp4. Radarr links it again. The hook deleted the broken "
     "upgrade, re-monitored it and marked the grab failed, so Radarr searches again."),
    (dict(RESTORED, code="regrabbed", kind="audio", n=1, failed_before=True), ", old file restored",
     "The hook put back the old file from the recycle bin: Film A (1979) HDTV-720p.mp4. Radarr links it again. The hook deleted the broken "
     "upgrade and re-monitored it. The grab was already marked failed with the rest of its download."),
    ({"code": "regrabbed", "name": "Radarr", "kind": "video", "n": 1, "failed_before": True}, ", re-grabbed",
     "The hook deleted the file and re-monitored it. The grab was already marked failed with the rest of its download."),
    ({"code": "regrabbed", "name": "Sonarr", "kind": "audio", "n": 3, "came": ["Show - s01e01 - HDTV-720p.mkv"], "linked": True, "own_copy": False,
      "others": 1, "stayed": None}, ", old file restored",
     "The hook put back the old file from the recycle bin: Show - s01e01 - HDTV-720p.mkv. Sonarr links it again. The hook deleted 3 broken files "
     "of this download, re-monitored them and marked the grab failed once, so Sonarr searches again. The old file of 1 more broken file of this "
     "download came back too."),
    ({"code": "regrabbed", "name": "Sonarr", "kind": "content", "n": 2, "came": ["a.mkv", "b.mkv"], "linked": False, "own_copy": True,
      "others": 2, "stayed": None}, ", old file restored",
     "The hook put back the old files from its own copy: a.mkv, b.mkv. Sonarr did not link them within 120 seconds, so rescan the item by hand. "
     "The hook deleted 2 files with the wrong content of this download, re-monitored them and marked the grab failed once, so Sonarr searches "
     "again. The old file of 2 more broken files of this download came back too."),
    (dict(NOTHING_BACK, code="regrabbed", kind="audio", n=1, stayed="the recycle bin no longer holds it"), ", re-grabbed",
     "The hook deleted the file, re-monitored it and marked the grab failed, so Radarr searches again. The old file did not come back: the "
     "recycle bin no longer holds it."),
    (dict(NOTHING_BACK, code="searched"), ", re-grabbed",
     "The hook deleted the broken import and sent Radarr a search for the item, because a manual import has no grab to mark failed. The old file "
     "did not come back: the recycle bin copy changed since its check."),
    (dict(NOTHING_BACK, code="deleted"), "",
     "The broken import is deleted. Its item was not monitored, so the hook sent no search. The old file did not come back: the recycle bin copy "
     "changed since its check."),
    (dict(RESTORED, code="restored"), ", old file restored",
     "The hook put back the old file from the recycle bin: Film A (1979) HDTV-720p.mp4. Radarr links it again. The hook deleted the broken "
     "import. It was a manual import, so no grab is marked failed and Radarr does not search."),
    (dict(NOTHING_BACK, code="no_grab", stayed="its recycle bin copy or its path changed before the delete"), "",
     "Radarr has no grab record for it, so the file stays. The old file did not come back: its recycle bin copy or its path changed before the "
     "delete."),
    ({"code": "no_grab", "name": "Radarr"}, "", "Radarr has no grab record for it, so the file stays."),
    ({"code": "would_regrab", "kind": "audio"}, ", would re-grab",
     "A re-grab would delete the file and search again. REGRAB does not list audio, so the file stays."),
    ({"code": "unconfirmed"}, ", not confirmed", "A second check did not find the same fault, so the file stays."),
    ({"code": "capped", "cap": 10}, "", "The cap of 10 re-grabs a day is reached, so the file stays."),
    ({"code": "failed", "step": "the delete", "error": "HTTPError: 500"}, "", "The re-grab stopped at the delete: HTTPError: 500"),
    ({"code": "failed", "manual": True, "step": "reading which items are monitored", "error": "x" * 300}, "",
     ("The restore stopped at reading which items are monitored: " + "x" * 300)[:300]),
    ({"code": "dry_run"}, "", "Dry run."),
    ({"code": "no_policy", "file": "Film A.mkv"}, "", "Skipped Film A.mkv. Fix the policy file."),
]

HARDLINKED = ("--apply would leave the file as it is, because it has another hard link, such as the download client's copy. Run --apply again "
              "after the other link is gone, for example after the download client removes its copy.")
# (block of subtitles.remux_block(), what --apply would do)
BLOCKS = [
    ({"code": "remux"}, "--apply would remux the file."),
    ({"code": "hardlinked", "why": "hardlinked"}, HARDLINKED),
    ({"code": "cap", "why": "over the 30 GB repack cap"}, "--apply would skip the remux, because the file is over the 30 GB repack cap. Raise "
                                                         "REPACK_MAX_GB to remux it."),
    ({"code": "space", "folder": "/m/Film", "free": 1.04, "need": 8.0}, "--apply would skip the remux, because /m/Film has 1.0 GB free, and the "
                                                                       "remux needs 8.0 GB there. Free space in /m/Film."),
    ({"code": "keep_root", "root": "/m/.kept", "user": "uid 80 and gid 81"}, "--apply would skip the remux, because uid 80 and gid 81 cannot write "
                                                                            "to /m/.kept, where it keeps the original. Make /m/.kept writable for "
                                                                            "them. In Docker, PUID and PGID set them."),
    ({"code": "keep_create", "root": "/m/.kept", "user": "uid 80 and gid 81"}, "--apply would skip the remux, because uid 80 and gid 81 cannot "
                                                                              "create /m/.kept, where it keeps the original. Create /m/.kept, "
                                                                              "writable for uid 80 and gid 81. In Docker, PUID and PGID set them."),
    ({"code": "keep", "why": "/m is on another file system with 0.1 GB free"}, "--apply would skip the remux, because it cannot keep the "
                                                                               "original. /m is on another file system with 0.1 GB free."),
]

STAYS = {"code": "stays", "track": "s1", "why": "the words differ", "gone": False, "result": None, "kept_back": None, "flags_off": True}
# (sentence of a subtitle alert, its text when done, its text when planned)
SUB_LINES = [
    ({"code": "removed", "track": "s2", "why": "the words differ", "by": "run", "kept": "/k/F.mkv"},
     "Subtitle track s2 does not match the audio, the words differ. This run removed it, and the original file is kept at /k/F.mkv.", None),
    (STAYS, "Subtitle track s1 does not match the audio, the words differ. It stays in the file, because the file was not remuxed. It loses its "
            "default and forced flags.",
     "Subtitle track s1 does not match the audio, the words differ. It stays in the file, because the file was not remuxed. --apply would turn "
     "its default and forced flags off."),
    (dict(STAYS, flags_off=False, kept_back="check"), "Subtitle track s1 does not match the audio, the words differ. It stays in the file, because "
                                                      "SUBTITLES is check, so the file stays as it is. Its flags stay.", None),
    (dict(STAYS, kept_back="keep_days"), "Subtitle track s1 does not match the audio, the words differ. It stays in the file, because "
                                         "KEEP_ORIGINALS_DAYS is 0, so the original could not be kept. It loses its default and forced flags.",
     "Subtitle track s1 does not match the audio, the words differ. The track would stay in the file, because a removal keeps the original, and "
     "KEEP_ORIGINALS_DAYS is 0. Set KEEP_ORIGINALS_DAYS above 0 to remove it. --apply would turn its default and forced flags off."),
    (dict(STAYS, gone=True, result="subtitle remux failed: x", block={"code": "remux"}, hardlinked=False),
     "Subtitle track s1 does not match the audio, the words differ. It stays in the file, because subtitle remux failed: x. It loses its default "
     "and forced flags.", "Subtitle track s1 does not match the audio, the words differ. --apply would remove it."),
    (dict(STAYS, gone=True, result="subtitle remux skipped, x", block={"code": "cap", "why": "over the 30 GB repack cap"}, hardlinked=False),
     "Subtitle track s1 does not match the audio, the words differ. It stays in the file, because subtitle remux skipped, x. It loses its default "
     "and forced flags.",
     "Subtitle track s1 does not match the audio, the words differ. --apply would skip the remux, because the file is over the 30 GB repack cap. "
     "Raise REPACK_MAX_GB to remux it. The track would stay in the file. --apply would turn its default and forced flags off."),
    (dict(STAYS, gone=True, result="subtitle remux skipped, hardlinked", block={"code": "hardlinked", "why": "hardlinked"}, hardlinked=True),
     "Subtitle track s1 does not match the audio, the words differ. It stays in the file, because subtitle remux skipped, hardlinked. It loses its "
     "default and forced flags.", f"Subtitle track s1 does not match the audio, the words differ. {HARDLINKED}"),
    ({"code": "sidecar", "name": "F.en.srt", "why": "the words differ", "kept": "/k/F.en.srt", "left": None},
     "The sidecar F.en.srt does not match the audio, the words differ. It moved to /k/F.en.srt. A program such as Bazarr can download it again.", None),
    ({"code": "sidecar", "name": "F.en.srt", "why": "the words differ", "kept": None, "left": None},
     "The sidecar F.en.srt does not match the audio, the words differ. It stays beside the file: a dry run. A program such as Bazarr can "
     "download it again.", None),
    ({"code": "converted_sidecar", "name": "F.fr.srt", "why": "the words differ", "kept": None, "left": "it does not read"},
     "The sidecar F.fr.srt does not match the audio, the words differ. The conversion left it out. It stays beside the file: it does not read.", None),
    ({"code": "converted_track", "track": "s1", "why": "the words differ", "kept": "/k/F.mp4"},
     "Subtitle track s1 does not match the audio, the words differ. The conversion left it out, and the original file is kept at /k/F.mp4.", None),
    ({"code": "sidecar_left", "name": "F.en.srt", "why": "a fix of +2.00 s", "left": "SUBTITLES is check, so the file stays as it is"},
     "The sidecar F.en.srt needs new times, a fix of +2.00 s, but it stays as it was: SUBTITLES is check, so the file stays as it is.", None),
    ({"code": "off", "track": "s2", "ref": None, "why": "the cues are off"}, "Subtitle track s2 is off the audio: the cues are off. Its times stay.", None),
    ({"code": "off", "track": "F.en.srt", "ref": "s1", "why": "the cues are off"},
     "Subtitle F.en.srt disagrees with the reference track s1: the cues are off. Its times stay.", None),
    ({"code": "off", "track": "s2", "ref": "F.en.srt", "why": "the cues are off"},
     "Subtitle track s2 disagrees with the reference sidecar F.en.srt: the cues are off. Its times stay.", None),
    ({"code": "not_retimed", "tracks": ["s1", "s2"], "result": "subtitle remux skipped, low space", "block": {"code": "remux"}},
     "Subtitle track s1, s2 needs new times, but subtitle remux skipped, low space. The file stays as it was.",
     "Subtitle track s1, s2 needs new times. --apply would remux the file."),
    ({"code": "check_times", "track": "s1", "why": "a fix of +2.00 s"}, "Subtitle s1 needs new times: a fix of +2.00 s. SUBTITLES is check, so its "
                                                                        "times stay.", None),
    ({"code": "check_flash", "track": "s1", "median": 0.254}, "Subtitle s1 flashes its cues: its median cue shows 0.25 s. SUBTITLES is check, so "
                                                              "its ends stay.", None),
    ({"code": "sweep", "far": [["s1", 3600.4, 1.5], ["s1", 3660.2, -1.25]]},
     "The sweep heard parts of the file off the fitted line: s1 at 1:00:00 by +1.50 s, s1 at 1:01:00 by -1.25 s. The times stay as the check "
     "decided.", None),
]


def test_every_template_has_a_golden():
    assert {f["kind"] for f, *_ in FINDINGS} == set(h.FINDINGS)
    assert {a["code"] for a, *_ in ACTIONS} == set(h.ACTIONS)
    assert {b["code"] for b, _ in BLOCKS} == set(h.BLOCKS)
    assert {x["code"] for x, *_ in SUB_LINES} == set(h.SUB_LINES)


@pytest.mark.parametrize("f, title, color, text", FINDINGS)
def test_each_finding_has_its_title_color_and_text(f, title, color, text):
    assert h.title(f) == (title, color)
    assert h.texts(f, "done")[0] == text


@pytest.mark.parametrize("a, end, text", ACTIONS)
def test_each_action_has_its_title_and_text(a, end, text):
    f = {"kind": "audio", "certain": SILENT, "action": a}
    assert h.title(f) == ("Broken audio" + end, "amber" if a["code"] == "unconfirmed" else "red")
    assert h.texts(f, "done") == ("All 3 audio samples are digital silence.", text)
    assert h.texts(f, "planned") == h.texts(f, "done")   # a re-grab runs only in an apply, so its words have one tense


@pytest.mark.parametrize("b, text", BLOCKS)
def test_each_dry_run_block_says_what_apply_would_do(b, text):
    assert h.block(b) == text


@pytest.mark.parametrize("x, done, planned", SUB_LINES)
def test_each_subtitle_sentence_in_each_tense(x, done, planned):
    assert h.sub_line(x, "done") == done
    assert h.sub_line(x, "planned") == (planned or done)


def test_a_template_that_fails_costs_only_its_text():
    """A finding that lacks a fact gives a line that says so, and the decision line and the other alerts still go out."""
    assert h.texts({"kind": "language"}, "done") == ("no text: KeyError: 'want'", None)
    assert h.alert_line({"kind": "language"}, "done") == "language: no text: KeyError: 'want'"


def decision(**kw):
    return dict(dict(id="65ab6056ca69", app="radarr", source="hook", apply=True, label="Film A (1979)", path="/m/Film A (1979)/Film A.mkv",
                     outcome="edited", result="edited", reasons=["kids_dub"], edits=[["track:=2", 0, 1]], tmdb="found",
                     findings=[{"kind": "language", "want": "English", "has": ["por"]},
                               {"kind": "audio", "certain": SILENT, "action": {"code": "capped", "cap": 10}}],
                     alert_kinds=["language", "audio"], **{"class": "English original: audio switched"}), **kw)


def test_the_log_target_is_the_decision_line(monkeypatch):
    monkeypatch.setattr(h, "VERSION", "0123456789ab")
    rec = h.render(dict(decision(), took=1.5), "log")
    assert {k: rec[k] for k in ("schema", "version", "host", "outcome", "took")} == {"schema": h.SCHEMA, "version": "0123456789ab",
                                                                                      "host": h.HOST, "outcome": "edited", "took": 1.5}
    assert rec["alerts"] == ["language: No audio track is English. The file has por.",
                             "audio: All 3 audio samples are digital silence. The cap of 10 re-grabs a day is reached, so the file stays."]
    assert h.render({"app": "radarr", "result": "x"}, "log")["outcome"] == "other" and "alerts" not in h.render({}, "log")
    assert h.render(dict(decision(), findings=[]), "log")["alerts"] == []


def test_the_logfmt_target_keeps_every_key():
    """Loki reads these keys with the syslog tag NAME, so they stay."""
    assert h.render(decision(), "logfmt") == ('arr=radarr source=hook outcome=edited class="English original: audio switched" edits=1 reasons=kids_dub '
                                             'alerts=language,audio tmdb=found label="Film A (1979)" id=65ab6056ca69')
    assert h.render({}, "logfmt") == 'arr="" source="" outcome=other class="" edits=0 reasons="" alerts="" tmdb=not_asked label="" id=""'


def test_the_embed_target_is_one_embed_per_finding():
    language, audio = h.render(decision(), "embed")
    assert {k: v for k, v in language.items() if k != "timestamp"} == {
        "title": "Wrong language", "description": "No audio track is English. The file has por.", "color": h.COLORS["amber"],
        "fields": [{"name": "Film A (1979)", "value": "Film A.mkv", "inline": False}], "footer": {"text": "TMDB found the item · arr-media-guard on host1"}}
    assert (audio["title"], audio["color"], audio["description"]) == (
        "Broken audio", h.COLORS["red"], "All 3 audio samples are digital silence.\nThe cap of 10 re-grabs a day is reached, so the file stays.")


def test_the_cli_target_is_the_backfill_line():
    rec = decision(apply=False, result="dry run", before=[{"sel": "track:=2", "pos": "a1"}], edits=[["track:=2", 0, 1], ["track:=3", 1, 0, "flag-forced"]],
                   heard={"a1": {"lang": "eng"}, "a2": {"lang": None}}, notes=["a1 is the original"], findings=[{"kind": "language", "want": "English",
                                                                                                                  "has": ["por"]}])
    assert h.render(rec, "cli") == ("dry run      Film A (1979) | a1 1->0, track:=3 flag-forced 0->1 | heard a1 eng, a2 no answer | a1 is the "
                                    "original | ALERT language: No audio track is English. The file has por.")
    rec = {"result": "would repack: the container is MP4/QuickTime", "label": "Film B", "header_repair": {"result": "would repair header: x"},
           "subcheck": {"s1": {"verdict": "mismatch", "why": "the words differ", "windows": [{"overlap": 0.05}, {"overlap": 0.1}], "timing": None}},
           "flash": {"s2": {"lengthened": 3, "cues": 40}}, "sidecars": [{"name": "B.en.srt", "action": "move", "result": "dry run"}],
           "subremux": {"codes": ["would_remux_subtitles"], "result": "would remux subtitles: remove track 2"},
           "repack": {"forced": "the packets differ", "not_forced": "x", "forced_name": "S01E02"}, "apply": False}
    assert h.render(rec, "cli") == ("would repack: the container is MP4/QuickTime Film B | header would repair header: x | subtitles s1 mismatch "
                                    "5%/10% (the words differ); s2 flashes, 3 of 40 ends lengthened; B.en.srt: move dry run | --apply would remux | "
                                    "forced, the proof refused: the packets differ | not forced: x | name forced: S01E02")
    assert "| would remux subtitles: remove track 2 |" in h.render(rec, "cli", "done")


def test_the_tense_comes_from_the_apply_flag_and_a_dry_run_says_what_apply_would_do(tmp_path):
    """A dry run renders planned, an apply done. The CLI line and the decision line of a dry run say the same."""
    line = dict(STAYS, kept_back="keep_days")
    rec = decision(apply=False, findings=[{"kind": "submatch", "lines": [line]}], alert_kinds=["submatch"])
    planned = "submatch: " + SUB_LINES[3][2]
    assert h.render(rec, "log")["alerts"] == [planned] and h.render(rec, "cli").endswith(f" | ALERT {planned}")
    assert h.render(rec, "log", "done")["alerts"] == ["submatch: " + SUB_LINES[3][1]]
    assert h.render(dict(rec, apply=True), "log")["alerts"] == ["submatch: " + SUB_LINES[3][1]]
    assert h.tense_of({}) == "done"
    with pytest.raises(ValueError):
        h.render(rec, "log", "past")
    with pytest.raises(ValueError):
        h.render(rec, "html")


def test_the_report_column_of_a_remux_names_what_apply_would_do():
    rec = {"subremux": {"codes": ["would_remux_subtitles"], "result": "would remux subtitles: remove track 2"}}
    assert (h.remux_column(rec, "planned"), h.remux_column(rec, "done")) == ("--apply would remux", "would remux subtitles: remove track 2")
    rec["subremux"]["codes"] = ["subtitle_remux_skipped"]
    assert h.remux_column(rec, "planned") == "--apply would skip the remux" and h.remux_column({}, "planned") == ""


UNDO = re.compile(r"## Undo an edit\n.*?```\n(.*?)```", re.S)


def test_the_documented_undo_reverses_an_edit_from_the_editing_line(tmp_path, settings):
    """docs/monitoring.md, "Undo an edit", runs the undo command of a film's last editing line. The editing line goes to
    the log before mkvpropedit runs, and its command brings back every old flag."""
    if not (shutil.which("ffmpeg") and shutil.which("mkvmerge") and shutil.which("mkvpropedit")):
        pytest.skip("needs ffmpeg and mkvtoolnix")
    src, path, log = tmp_path / "src.mkv", tmp_path / "Film A (2000).mkv", tmp_path / "log.jsonl"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
                    "-map", "0", "-map", "1", "-c:a", "aac", str(src)], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(path), "--language", "0:eng", "--default-track-flag", "0:1", "--language", "1:spa",
                    "--default-track-flag", "1:0", str(src)], check=True)
    settings(log=str(log), state_dir=str(tmp_path))
    flags = lambda: [(t["properties"]["language"], t["properties"]["default_track"]) for t in h.mkvmerge(str(path))["tracks"]]
    j, before = h.mkvmerge(str(path)), flags()
    a1, a2 = (f'track:={t["properties"]["uid"]}' for t in j["tracks"])   # mkvpropedit selects a track by its UID
    rec = h.edit({"path": str(path), "label": "Film A (2000)"}, j, [[a1, 0, 1], [a2, 1, 0]], True)
    assert rec["outcome"] == "edited" and flags() == [("eng", False), ("spa", True)] and before == [("eng", True), ("spa", False)]
    (editing,) = [r for r in map(json.loads, log.read_text().splitlines()) if r.get("result") == "editing"]
    assert editing["undo"] == ["mkvpropedit", str(path), "--edit", a1, "--set", "flag-default=1", "--edit", a2, "--set", "flag-default=0"]
    procedure = UNDO.search(open(os.path.join(ROOT, "docs", "monitoring.md")).read()).group(1)
    script = procedure.split("<<'PY'\n", 1)[1].rsplit("PY\n", 1)[0].replace("/var/log/arr-media-guard.jsonl", str(log))
    subprocess.run([sys.executable, "-c", script], check=True)
    assert flags() == before
