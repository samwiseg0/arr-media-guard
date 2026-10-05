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
import ast
import contextlib
import dataclasses
import io
import json
import os
import re
import shutil
import subprocess
import sys
import types
import urllib.error
import urllib.request

import pytest

import amg

os.environ["ARR_MEDIA_GUARD_ENV"] = "/nonexistent/arr-media-guard.env"
h = amg.load("arr_media_guard_report")
h.CFG = dataclasses.replace(h.CFG, instance="host1", name="arr-media-guard")
HOOK = "https://discord.example/api/webhooks/1/t0ken-0123456789"   # a made-up webhook
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SILENT = "the audio is silent at all 3 places checked"
RESTORED = {"name": "Radarr", "came": ["Film A (1979) HDTV-720p.mp4"], "linked": True, "own_copy": False, "others": 0, "stayed": None}
NOTHING_BACK = {"name": "Radarr", "came": [], "linked": True, "own_copy": True, "others": 0, "stayed": "the recycle bin copy changed since its check"}
LANGS = {"a1": "eng", "a2": "eng", "s1": "eng", "s2": "spa", "s3": "eng"}   # the language of each track place, see report.track_langs()

# (finding, alert title, color, text in the done tense). LANGS names the track languages.
FINDINGS = [
    ({"kind": "language", "want": "English", "has": ["por"]}, "Wrong audio language", "amber", "The audio is Portuguese, but it should be English."),
    ({"kind": "language", "want": "English or Spanish", "has": ["por", "fre"]}, "Wrong audio language", "amber",
     "The audio is French and Portuguese, but it should be English or Spanish."),
    ({"kind": "runtime", "runs": "10:05", "listed": 62}, "Wrong runtime", "amber", "The file runs 10:05, but the listed runtime is 62 minutes."),
    ({"kind": "duration", "why": "The file says it runs 3:05:08, but the video and audio stop at 2:03:26. Players may show the wrong length."},
     "Wrong length in the file", "amber", "The file says it runs 3:05:08, but the video and audio stop at 2:03:26. Players may show the wrong "
     "length."),
    ({"kind": "episode", "imported": [["S04E15", "Ship Voyage"]], "said": "the release's NFO", "title": "Squidtastic Voyage/That's No Lady",
      "names": "S04E23 and S04E27"}, "Maybe the wrong episode", "amber",
     "Imported as S04E15 \"Ship Voyage\". The release's NFO calls it \"Squidtastic Voyage/That's No Lady\", which is S04E23 and S04E27."),
    ({"kind": "episode", "imported": [["S01E02", "Overnight"]], "said": "the release name", "title": "Anxious Times at Show Alpha",
      "names": "S01E03"}, "Maybe the wrong episode", "amber",
     "Imported as S01E02 \"Overnight\". The release name calls it \"Anxious Times at Show Alpha\", which is S01E03."),
    ({"kind": "episode", "imported": [["S01E05", None], ["S01E06", "Two"]], "said": "the file name", "title": "Three", "names": "S01E07"},
     "Maybe the wrong episode", "amber", "Imported as S01E05 and S01E06 \"Two\". The file name calls it \"Three\", which is S01E07."),
    ({"kind": "content", "signals": ["the audio is Portuguese, but it should be English",
     "the release name says 2017, but the listed year is 1979"], "scored": ["language", "year"], "points": 2}, "Wrong content", "amber",
     "The audio is Portuguese, but it should be English. The release name says 2017, but the listed year is 1979."),
    ({"kind": "audio", "doubts": ["the audio is silent at 1 of 3 places checked", "no audio plays at an earlier place checked"]},
     "Audio may be broken", "amber", "The audio is silent at 1 of 3 places checked. No audio plays at an earlier place checked."),
    ({"kind": "video", "doubts": ["no video at 2:30"]}, "Video may be broken", "amber", "No video at 2:30."),
    ({"kind": "audio", "certain": "the audio is silent at all 3 places checked", "action": {"code": "dry_run"}}, "Broken audio", "red",
     "The audio is silent at all 3 places checked."),
    ({"kind": "video", "certain": "the video is broken at 2 of 3 places checked: 2 playback errors at 6:10 and no video at 31:24",
     "action": {"code": "unconfirmed"}}, "Broken video, not confirmed", "amber",
     "The video is broken at 2 of 3 places checked: 2 playback errors at 6:10 and no video at 31:24."),
    ({"kind": "damage", "fault": "part of the audio cannot be read",
     "line": "This audio track contains 4096 bytes of invalid data which were skipped", "refusal": "the packet data of stream audio 1 (mp3) differ",
     "action": {"code": "capped", "cap": 10}}, "Damaged file", "red",
     "Converting the file to MKV showed that it's damaged, because part of the audio cannot be read. This "
     "audio track contains 4096 bytes of invalid data which were skipped. The new MKV did not match the "
     "original, because the packet data of stream audio 1 (mp3) differ."),
    ({"kind": "damage", "fault": "parts of the file cannot be read", "line": "Invalid data found when processing input", "refusal": None,
     "action": {"code": "would_regrab", "kind": "damage"}}, "Damaged file, re-grab is off", "red",
     "Converting the file to MKV showed that it's damaged, because parts of the file cannot be read. Invalid data found when processing input."),
    ({"kind": "repack", "container": "MP4/QuickTime", "why": "mkvmerge exited 2: x"}, "Conversion to MKV failed", "amber",
     "Couldn't convert the MP4/QuickTime file to MKV, so the original was kept. Mkvmerge exited 2: x."),
    ({"kind": "repack", "state": "converted", "note": "a conversion to MKV finished, and its 2 extras wait in .hide for the next --convert run"},
     "Stopped conversion", "amber", "A conversion to MKV finished, and its 2 extras wait in .hide for the next --convert run."),
    ({"kind": "repack", "state": "held",
     "note": "a conversion to MKV stopped partway, after it hid the original. The original may be hidden as /m/.F.avi.held, "
     "the new file is /m/F.mkv, and 0 extras may be hidden in .hide"}, "Stopped conversion", "amber",
     "A conversion to MKV stopped partway, after it hid the original. The original may be hidden as /m/.F.avi.held, "
     "the new file is /m/F.mkv, and 0 extras may be hidden in .hide."),
    ({"kind": "header", "why": "the size changed from 1000 to 2000 bytes"}, "File repair failed", "amber",
     "Couldn't repair the file, so it was left as it is. The size changed from 1000 to 2000 bytes."),
    ({"kind": "cut", "why": "the video and audio stop at 21:11, but the listed runtime is 25 minutes, and the subtitles run to 23:05"},
     "File may be cut short", "amber", "The video and audio stop at 21:11, but the listed runtime is 25 minutes, and the subtitles run to "
     "23:05. The file may be cut short, or the subtitles may belong to another version. Nothing was changed."),
    ({"kind": "subtitle", "issue": ["a subtitle event runs to 13:23:33, past the video and the audio at 1:54:33"], "tracks": [{"track": "s3",
     "codec": "S_HDMV/PGS", "end": 48213.4, "streams": 6873.9}]}, "Subtitles run past the end", "amber",
     "The English subtitles (track 3) keep going until 13:23:33, but the video and audio end at 1:54:33. "
     "They're in PGS format, which can't be trimmed automatically, so they were left as they are."),
    ({"kind": "sublang", "mismatch": ["subtitle track 1 is tagged English, but its text reads as Romanian"],
     "muted": ["s1 loses its default and forced flags, x"]}, "Subtitle language may be wrong", "amber",
     "Subtitle track 1 is tagged English, but its text reads as Romanian. Nothing else confirms another "
     "language, so the tag was kept. Turned off its default and forced flags."),
    ({"kind": "sublang", "mismatch": ["subtitle track 1 is tagged English, but its text reads as Romanian"], "muted": []},
     "Subtitle language may be wrong", "amber", "Subtitle track 1 is tagged English, but its text reads as Romanian. Nothing else confirms another "
     "language, so the tag was kept."),
    ({"kind": "edit", "error": "VERIFY FAILED, flags did not change", "unread": None, "on": ["a2 eng", "s1 eng"]}, "Track flag change failed",
     "amber", "Couldn't change which tracks play by default. The edit ran, but the flags did not change. The file "
     "still opens, and its default tracks are the English audio (track 2) and the English subtitles (track 1)."),
    ({"kind": "edit", "error": "mkvpropedit failed: x", "unread": "RuntimeError: mkvmerge: could not open the file", "on": None},
     "Track flag change failed", "amber", "Couldn't change which tracks play by default. Mkvpropedit failed: x. The file no longer opens. "
     "RuntimeError: mkvmerge: could not open the file"),
    ({"kind": "edit", "error": "mkvpropedit failed: x", "unread": None, "on": None}, "Track flag change failed", "amber",
     "Couldn't change which tracks play by default. Mkvpropedit failed: x."),
    ({"kind": "edit", "error": "VERIFY FAILED, flags did not change", "unread": None, "on": []}, "Track flag change failed", "amber",
     "Couldn't change which tracks play by default. The edit ran, but the flags did not change. The file "
     "still opens, and no track plays by default."),
    ({"kind": "policy", "file": "/etc/arr-media-guard/policy.json", "error": "line 3: a comma is missing", "action": {"code": "no_policy",
     "file": "Film A.mkv"}}, "Policy file didn't load", "amber",
     "/etc/arr-media-guard/policy.json didn't load, so no tracks are changed until it's fixed. line 3: a comma is missing"),
    ({"kind": "submatch", "lines": [{"code": "removed", "track": "s1", "why": "the words differ", "by": "hook", "kept": "/k/Film.mkv"}]},
     "Wrong subtitles", "amber", "The English subtitles (track 1) don't match what's said in the audio. Removed them and kept the "
     "original file at /k/Film.mkv."),
    ({"kind": "subtiming", "lines": [{"code": "check_flash", "track": "s1", "median": 0.25}, {"code": "sweep", "far": [["s1", 3600, 1.5]]}]},
     "Subtitles out of sync", "amber", "The English subtitles (track 1) flash by too fast to read. Half the lines show for 0.25 s or less. "
     "SUBTITLES is set to check, so they were left as they are. The English subtitles (track 1) are about "
     "1.5 s late at 1:00:00. They were left as they are."),
    ({"kind": "subtitle", "issue": ["a subtitle event runs to 26:01, past the video and the audio at 23:52"], "tracks": [{"track": "s2",
     "codec": "S_TEXT/ASCII", "end": 1561.0, "streams": 1432.0}]}, "Subtitles run past the end", "amber",
     "The Spanish subtitles (track 2) keep going until 26:01, but the video and audio end at 23:52. "
     "They're in a format that can't be trimmed automatically, so they were left as they are."),
]

# (action, the end it gives the title of broken audio, its text). Each text ends with the restore, see restored().
ACTIONS = [
    ({"code": "regrabbed", "name": "Radarr", "kind": "audio", "n": 1}, ", re-grabbed",
     "Deleted the broken file and marked the grab as failed, so Radarr is searching for another copy."),
    ({"name": "Radarr", "came": ["Film A (1979) HDTV-720p.mp4"], "linked": True, "own_copy": False, "others": 0, "stayed": None,
     "code": "regrabbed", "kind": "audio", "n": 1}, ", old file restored",
     "Deleted the broken upgrade and marked the grab as failed, so Radarr is searching for another copy. "
     "Put back the old file from the recycle bin: Film A (1979) HDTV-720p.mp4. Radarr picked it up again."),
    ({"name": "Radarr", "came": ["Film A (1979) HDTV-720p.mp4"], "linked": True, "own_copy": False, "others": 0, "stayed": None,
     "code": "regrabbed", "kind": "audio", "n": 1, "failed_before": True}, ", old file restored",
     "Deleted the broken upgrade. Its download was already marked as failed, so Radarr is already "
     "searching for another copy. Put back the old file from the recycle bin: Film A (1979) HDTV-720p.mp4. Radarr picked it up again."),
    ({"code": "regrabbed", "name": "Radarr", "kind": "video", "n": 1, "failed_before": True}, ", re-grabbed",
     "Deleted the broken file. Its download was already marked as failed, so Radarr is already searching for another copy."),
    ({"code": "regrabbed", "name": "Sonarr", "kind": "audio", "n": 3, "came": ["Show - s01e01 - HDTV-720p.mkv"], "linked": True, "own_copy": False,
     "others": 1, "stayed": None}, ", old file restored",
     "Deleted 3 broken files from this download and marked the grab as failed, so Sonarr is searching for "
     "other copies. Put back the old file from the recycle bin: Show - s01e01 - HDTV-720p.mkv. Sonarr "
     "picked it up again. Also put back the old file of 1 more broken file from this download."),
    ({"code": "regrabbed", "name": "Sonarr", "kind": "content", "n": 2, "came": ["a.mkv", "b.mkv"], "linked": False, "own_copy": True, "others": 2,
     "stayed": None}, ", old file restored", "Deleted 2 files with the wrong content from this download and marked the grab as failed, so Sonarr "
     "is searching for other copies. Put back the old files from the kept copies: a.mkv, b.mkv. Sonarr "
     "didn't pick them up within 120 seconds. Also put back the old files of 2 more broken files from this download."),
    ({"name": "Radarr", "came": [], "linked": True, "own_copy": True, "others": 0, "stayed": "the recycle bin no longer holds it",
     "code": "regrabbed", "kind": "audio", "n": 1}, ", re-grabbed",
     "Deleted the broken file and marked the grab as failed, so Radarr is searching for another copy. The "
     "old file wasn't put back, because the recycle bin no longer holds it."),
    ({"name": "Radarr", "came": [], "linked": True, "own_copy": True, "others": 0, "stayed": "the recycle bin copy changed since its check",
     "code": "searched"}, ", re-grabbed", "Deleted the broken file and asked Radarr to search for another copy. It was a manual import, so "
     "there was no grab to mark as failed. The old file wasn't put back, because the recycle bin copy changed since its check."),
    ({"name": "Radarr", "came": [], "linked": True, "own_copy": True, "others": 0, "stayed": "the recycle bin copy changed since its check",
     "code": "deleted"}, "", "Deleted the broken file. Its item isn't monitored, so no search was started. The old file wasn't put "
     "back, because the recycle bin copy changed since its check."),
    ({"name": "Radarr", "came": ["Film A (1979) HDTV-720p.mp4"], "linked": True, "own_copy": False, "others": 0, "stayed": None,
     "code": "restored"}, ", old file restored",
     "Deleted the broken file. It was a manual import, so there was no grab to mark as failed, and Radarr "
     "won't search for another copy. Put back the old file from the recycle bin: Film A (1979) HDTV-720p.mp4. Radarr picked it up again."),
    ({"name": "Radarr", "came": [], "linked": True, "own_copy": True, "others": 0,
     "stayed": "its recycle bin copy or its path changed before the delete", "code": "no_grab"}, "",
     "Radarr has no record of grabbing it, so the file was kept. The old file wasn't put back, because its "
     "recycle bin copy or its path changed before the delete."),
    ({"code": "no_grab", "name": "Radarr"}, "", "Radarr has no record of grabbing it, so the file was kept."),
    ({"code": "would_regrab", "kind": "audio"}, ", re-grab is off", "Re-grabs for broken audio are off, so the file was kept."),
    ({"code": "unconfirmed"}, ", not confirmed", "A second check didn't find the same problem, so the file was kept."),
    ({"code": "capped", "cap": 10}, "", "The limit of 10 re-grabs a day was reached, so the file was kept."),
    ({"code": "failed", "step": "the delete", "error": "HTTPError: 500"}, "", "The re-grab failed during the delete. HTTPError: 500"),
    ({"code": "failed", "manual": True, "step": "reading which items are monitored", "error": "x" * 300}, "",
     ("The restore failed during reading which items are monitored. " + "x" * 300)[:300]),
    ({"code": "dry_run"}, "", "Dry run, so nothing was changed."),
    ({"code": "no_policy", "file": "Film A.mkv"}, "", "Skipped Film A.mkv."),
    ({"name": "Radarr", "came": [], "linked": True, "own_copy": True, "others": 1, "stayed": "the recycle bin copy changed since its check",
     "code": "restored"}, "", "Deleted the broken file. It was a manual import, so there was no grab to mark as failed, and Radarr won't search "
     "for another copy. Also put back the old file of 1 more broken file from this download. The old file wasn't put back, because the recycle "
     "bin copy changed since its check."),
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
     "The Spanish subtitles (track 2) don't match what's said in the audio. Removed them and kept the original file at /k/F.mkv.", None),
    ({"code": "stays", "track": "s1", "why": "the words differ", "gone": False, "result": None, "kept_back": None, "flags_off": True},
     "The English subtitles (track 1) don't match what's said in the audio. They're still in the file, "
     "because the run could not remove them. Turned off their default and forced flags.",
     "The English subtitles (track 1) don't match what's said in the audio. They're still in the file, "
     "because the run could not remove them. --apply would turn their default and forced flags off."),
    ({"code": "stays", "track": "s1", "why": "the words differ", "gone": False, "result": None, "kept_back": "check", "flags_off": False},
     "The English subtitles (track 1) don't match what's said in the audio. They're still in the file, "
     "because SUBTITLES is set to check. Their flags were left as they are.", None),
    ({"code": "stays", "track": "s1", "why": "the words differ", "gone": False, "result": None, "kept_back": "keep_days", "flags_off": True},
     "The English subtitles (track 1) don't match what's said in the audio. They're still in the file, "
     "because KEEP_ORIGINALS_DAYS is 0, and a removal needs a copy of the original. Turned off their default and forced flags.",
     "The English subtitles (track 1) don't match what's said in the audio. They would stay in the file, "
     "because a removal keeps the original, and KEEP_ORIGINALS_DAYS is 0. Set KEEP_ORIGINALS_DAYS above 0 "
     "to remove them. --apply would turn their default and forced flags off."),
    ({"code": "stays", "track": "s1", "why": "the words differ", "gone": True, "result": "subtitle remux failed: mkvmerge exited 2",
     "kept_back": None, "flags_off": True, "block": {"code": "remux"}, "hardlinked": False},
     "The English subtitles (track 1) don't match what's said in the audio. They're still in the file, "
     "because rewriting the file failed (mkvmerge exited 2). Turned off their default and forced flags.",
     "The English subtitles (track 1) don't match what's said in the audio. --apply would remove them."),
    ({"code": "stays", "track": "s1", "why": "the words differ", "gone": True,
     "result": "subtitle remux skipped, over the 30 GB repack cap: remove track 3", "kept_back": None, "flags_off": True, "block": {"code": "cap",
     "why": "over the 30 GB repack cap"}, "hardlinked": False},
     "The English subtitles (track 1) don't match what's said in the audio. They're still in the file, "
     "because the file is over the 30 GB limit of REPACK_MAX_GB. Turned off their default and forced flags.",
     "The English subtitles (track 1) don't match what's said in the audio. --apply would skip the remux, "
     "because the file is over the 30 GB repack cap. Raise REPACK_MAX_GB to remux it. They would stay in "
     "the file. --apply would turn their default and forced flags off."),
    ({"code": "stays", "track": "s1", "why": "the words differ", "gone": True, "result": "subtitle remux skipped, hardlinked: remove track 3",
     "kept_back": None, "flags_off": True, "block": {"code": "hardlinked", "why": "hardlinked"}, "hardlinked": True},
     "The English subtitles (track 1) don't match what's said in the audio. They're still in the file, "
     "because the file has another hard link, such as the download client's copy. Turned off their default and forced flags.",
     "The English subtitles (track 1) don't match what's said in the audio. --apply would leave the file "
     "as it is, because it has another hard link, such as the download client's copy. Run --apply again "
     "after the other link is gone, for example after the download client removes its copy."),
    ({"code": "sidecar", "name": "F.en.srt", "why": "the words differ", "kept": "/k/F.en.srt", "left": None},
     "The subtitles in F.en.srt don't match what's said in the audio. Moved the file to /k/F.en.srt.", None),
    ({"code": "sidecar", "name": "F.en.srt", "why": "the words differ", "kept": None, "left": None},
     "The subtitles in F.en.srt don't match what's said in the audio. The file was left beside the video, because this is a dry run.", None),
    ({"code": "converted_sidecar", "name": "F.fr.srt", "why": "the words differ", "kept": None, "left": "it does not read"},
     "The subtitles in F.fr.srt don't match what's said in the audio, so the conversion to MKV left them "
     "out. The file was left beside the video, because it does not read.", None),
    ({"code": "converted_track", "track": "s1", "why": "the words differ", "kept": "/k/F.mp4"},
     "Subtitle track 1 of the original file doesn't match what's said in the audio, so the conversion to "
     "MKV left it out. The original file is kept at /k/F.mp4.", None),
    ({"code": "sidecar_left", "name": "F.en.srt", "why": "a fix of +2.00 s", "left": "SUBTITLES is set to check", "action": "retime"},
     "The subtitles in F.en.srt are out of sync, but the file was left as it is, because SUBTITLES is set to check.", None),
    ({"code": "off", "track": "s2", "ref": None, "why": "the cues are off", "offsets": [2.55, 5.28], "unfixed": None},
     "The Spanish subtitles (track 2) are out of sync by different amounts in different parts of the file: "
     "2.5 s and 5.3 s late. One shift can't fix that, so they were left as they are.", None),
    ({"code": "off", "track": "F.en.srt", "ref": "s1", "why": "the cues are off", "offsets": None, "unfixed": -2.04},
     "The subtitles in F.en.srt seem about 2.0 s early compared with the English subtitles (track 1), but "
     "no fix lined them up well enough, so they were left as they are.", None),
    ({"code": "off", "track": "s2", "ref": "F.en.srt", "why": "the cues are off"},
     "The Spanish subtitles (track 2) are out of sync compared with the subtitles in F.en.srt, but no fix "
     "lined them up well enough, so they were left as they are.", None),
    ({"code": "not_retimed", "tracks": ["s1", "s3"], "result": "subtitle remux skipped, low space: 1.0 GB free for 8.0 GB: track 2: +2.000 s",
     "block": {"code": "remux"}}, "Subtitle tracks 1 and 3 (English) need new times, but the fix failed, because only 1.0 GB is free "
     "for the 8.0 GB file. The file was left as it is.", "Subtitle tracks 1 and 3 (English) need new times. --apply would remux the file."),
    ({"code": "not_retimed", "tracks": ["s1"],
     "result": "subtitle remux failed, the original changed: the app replaced or renamed the original during the subtitle remux",
     "block": {"code": "remux"}}, "The English subtitles (track 1) need new times, but the fix failed, because the app replaced or "
     "renamed the file at the same time. The file was left as it is.",
     "The English subtitles (track 1) need new times. --apply would remux the file."),
    ({"code": "check_times", "track": "s1", "why": "a fix of +2.00 s", "fix": {"offset": 2.0, "rate": "1/1"}},
     "The English subtitles (track 1) are about 2.0 s late. SUBTITLES is set to check, so they were left as they are.", None),
    ({"code": "check_times", "track": "s2", "why": "a fix of -1.25 s and the ratio 25/24", "fix": {"offset": -1.25, "rate": "25/24"}},
     "The Spanish subtitles (track 2) are about 1.2 s early at the start and drift over time. SUBTITLES is "
     "set to check, so they were left as they are.", None),
    ({"code": "check_flash", "track": "s1", "median": 0.254},
     "The English subtitles (track 1) flash by too fast to read. Half the lines show for 0.25 s or less. "
     "SUBTITLES is set to check, so they were left as they are.", None),
    ({"code": "sweep", "far": [["s1", 3600.4, 1.5], ["s1", 3660.2, -1.25]]},
     "The English subtitles (track 1) are out of sync: 1.5 s late at 1:00:00 and 1.2 s early at 1:01:00. They were left as they are.", None),
    ({"code": "sweep", "far": [["s1", 257.8, 139.06], ["s1", 499.9, 138.87], ["s1", 725.2, 138.43], ["s1", 900.0, 138.68]]},
     "The English subtitles (track 1) are about 2 min 19 s late at 4:17, 8:19, 12:05 and 15:00. They were left as they are.", None),
    ({"code": "sweep", "far": [["s1", 1366.4, 2.54], ["s1", 1381.6, 1.66], ["s3", 1366.4, 2.54], ["s3", 1381.6, 1.66]]},
     "Subtitle tracks 1 and 3 (English) are late by 2.5 s at 22:46 and 1.7 s at 23:01. They were left as they are.", None),
    ({"code": "garbled", "tracks": ["s2"], "repair": True, "flags_off": True, "result": "subtitle remux failed: mkvmerge exited 2",
      "block": {"code": "remux"}, "hardlinked": False},
     "The Spanish subtitles (track 2) show garbled characters, but the fix failed, because rewriting the file failed (mkvmerge exited 2). The "
     "file was left as it is.", "The Spanish subtitles (track 2) show garbled characters. --apply would remux the file."),
    ({"code": "garbled", "tracks": ["s1", "s3"], "repair": True, "flags_off": False},
     "Subtitle tracks 1 and 3 (English) show garbled characters. SUBTITLES is set to check, so they were left as they are.", None),
    ({"code": "garbled", "tracks": ["s2"], "repair": False, "flags_off": True},
     "The Spanish subtitles (track 2) show garbled characters, but the right text couldn't be worked out for sure, so they were left as they "
     "are.", None),
    ({"code": "repaired", "tracks": ["s1", "s2"], "kept": "/k/F.mkv"},
     "The English subtitles (track 1) and the Spanish subtitles (track 2) showed garbled characters. Replaced them with the same subtitles "
     "in readable characters and kept the original file at /k/F.mkv.", None),
    ({"code": "removed", "track": "s3", "why": "a lift of 0.08", "by": "run", "kept": "/k/F.mkv", "layout": True},
     "The English subtitles (track 3) don't line up with the speech in the audio, so they may be from another episode or "
     "version. Removed them and kept the original file at /k/F.mkv.", None),
    ({**STAYS, "layout": True, "kept_back": "check", "flags_off": False},
     "The English subtitles (track 1) don't line up with the speech in the audio, so they may be from another episode or "
     "version. They're still in the file, because SUBTITLES is set to check. Their flags were left as they are.", None),
    ({"code": "sidecar", "name": "F.es.srt", "why": "a lift of 0.08", "kept": "/k/F.es.srt", "left": None, "layout": True},
     "The subtitles in F.es.srt don't line up with the speech in the audio, so they may be from another episode or "
     "version. Moved the file to /k/F.es.srt.", None),
    ({"code": "layout", "track": "s3"},
     "The English subtitles (track 3) don't line up with the speech in the audio, so they may be from another episode or "
     "version. They were left as they are.", None),
    ({"code": "layout", "track": "F.es.srt"},
     "The subtitles in F.es.srt don't line up with the speech in the audio, so they may be from another episode or "
     "version. They were left as they are.", None),
    ({"code": "live", "track": "s1", "lag": 8.0, "moved": 300, "cues": 400, "left": 100, "flags_off": True},
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 8.0 s late. Moved 300 of 400 lines to their speech. 100 lines could not be timed and were left as they are.",
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 8.0 s late. --apply would move 300 of 400 lines to their speech. 100 lines could not be timed and were left as they are."),
    ({"code": "live", "track": "s1", "lag": 8.0, "moved": 300, "cues": 400, "left": 100, "flags_off": True, "block": {"code": "remux"}},
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 8.0 s late. Moved 300 of 400 lines to their speech. 100 lines could not be timed and were left as they are.",
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 8.0 s late. --apply would move 300 of 400 lines to their speech. 100 lines could not be timed and were left as they are."),
    ({"code": "live", "track": "s1", "lag": 8.0, "moved": 300, "cues": 400, "left": 100, "flags_off": True, "block": {"code": "cap", "why": "over the 30 GB repack cap"}},
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 8.0 s late. Moved 300 of 400 lines to their speech. 100 lines could not be timed and were left as they are.",
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 8.0 s late. --apply would move 300 of 400 lines to their speech. 100 lines could not be timed and were left as they are. "
     "--apply would skip the remux, because the file is over the 30 GB repack cap. Raise REPACK_MAX_GB to remux it."),
    ({"code": "live", "track": "s1", "lag": 0.6, "moved": 0, "cues": 400, "left": 350, "flags_off": True},
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 0.6 s late. None of their lines could be matched to the speech, so they were left as they are.", None),
    ({"code": "live", "track": "s1", "lag": 8.0, "moved": 380, "cues": 400, "left": 10, "flags_off": False},
     "The English subtitles (track 1) run behind the speech by a different amount on each line, as live captions do. On average they "
     "are about 8.0 s late. SUBTITLES is set to check, so they were left as they are.", None),
    ({"code": "stripped", "track": "s2", "name": "F.spa.garbled.txt", "kept": "/k/F.mkv"},
     "The Spanish subtitles (track 2) showed garbled characters that couldn't be fixed. Took them out of the file and kept their text "
     "beside the video as F.spa.garbled.txt. The original file is kept at /k/F.mkv.", None),
    ({"code": "garbled", "tracks": ["s2"], "repair": False, "flags_off": True, "result": "subtitle remux failed: mkvmerge exited 2",
      "names": ["F.spa.garbled.txt"], "block": {"code": "remux"}, "hardlinked": False},
     "The Spanish subtitles (track 2) show garbled characters, but the fix failed, because rewriting the file failed (mkvmerge exited 2). The "
     "file was left as it is.", "The Spanish subtitles (track 2) show garbled characters, and the right text couldn't be worked out for sure. "
     "--apply would take them out of the file and keep their text beside the video as F.spa.garbled.txt."),
    ({"code": "garbled", "tracks": ["s2"], "repair": False, "flags_off": True, "kept_back": "keep_days"},
     "The Spanish subtitles (track 2) show garbled characters, and the right text couldn't be worked out for sure. They're still in the "
     "file, because KEEP_ORIGINALS_DAYS is 0, and a removal needs a copy of the original.", None),
    ({"code": "off", "track": "s2", "ref": None, "why": "the cues are off", "offsets": [2.05, 2.05, 10.05], "unfixed": None, "layout": True},
     "The Spanish subtitles (track 2) line up with the speech at different times in different parts of the file, between 2.0 s late "
     "and 10.1 s late. They may be from another version, so they were left as they are.", None),
]

def test_every_template_has_a_golden():
    assert {f["kind"] for f, *_ in FINDINGS} == set(h.FINDINGS)
    assert {a["code"] for a, *_ in ACTIONS} == set(h.ACTIONS)
    assert {b["code"] for b, _ in BLOCKS} == set(h.BLOCKS)
    assert {x["code"] for x, *_ in SUB_LINES} == set(h.SUB_LINES)


@pytest.mark.parametrize("f, title, color, text", FINDINGS)
def test_each_finding_has_its_title_color_and_text(f, title, color, text):
    assert h.title(f) == (title, color)
    assert h.texts(f, "done", LANGS)[0] == text


@pytest.mark.parametrize("a, end, text", ACTIONS)
def test_each_action_has_its_title_and_text(a, end, text):
    f = {"kind": "audio", "certain": SILENT, "action": a}
    assert h.title(f) == ("Broken audio" + end, "amber" if a["code"] == "unconfirmed" else "red")
    assert h.texts(f, "done") == ("The audio is silent at all 3 places checked.", text)
    assert h.texts(f, "planned") == h.texts(f, "done")   # a re-grab runs only in an apply, so its words have one tense


@pytest.mark.parametrize("b, text", BLOCKS)
def test_each_dry_run_block_says_what_apply_would_do(b, text):
    assert h.block(b) == text


@pytest.mark.parametrize("x, done, planned", SUB_LINES)
def test_each_subtitle_sentence_in_each_tense(x, done, planned):
    assert h.unmarked(h.sub_line(x, "done", LANGS)) == done
    assert h.unmarked(h.sub_line(x, "planned", LANGS)) == (planned or done)


def test_a_language_has_its_english_name():
    """A code in ALIAS takes the name of its set, an unknown code stays as it is."""
    assert [h.arr_decide.lang_name(c) for c in ("fre", "gle", "zho", "per", "und", None, "xyz")] == \
        ["French", "Irish", "Chinese", "Persian", "untagged", "untagged", "xyz"]
    assert h.arr_decide.lang_names(["eng", "jpn", "jap"], "or") == "English or Japanese" and h.late_by([2.54, -1.25]) == "2.5 s late and 1.2 s early"


def test_a_template_that_fails_costs_only_its_text():
    """A finding that lacks a fact gives a line that says so, and the decision line and the other alerts still go out."""
    assert h.texts({"kind": "language"}, "done") == ("no text: KeyError: 'has'", None)
    assert h.alert_line({"kind": "language"}, "done") == "language: no text: KeyError: 'has'"


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
    assert rec["alerts"] == ["language: The audio is Portuguese, but it should be English.",
                             "audio: The audio is silent at all 3 places checked. The limit of 10 re-grabs a day was reached, so the file was kept."]
    assert h.render({"app": "radarr", "result": "x"}, "log")["outcome"] == "other" and "alerts" not in h.render({}, "log")
    assert h.render(dict(decision(), findings=[]), "log")["alerts"] == []


def test_the_logfmt_target_keeps_every_key():
    """Loki reads these keys with the syslog tag NAME, so they stay."""
    assert h.render(decision(), "logfmt") == ('arr=radarr source=hook outcome=edited class="English original: audio switched" edits=1 reasons=kids_dub '
                                             'alerts=language,audio tmdb=found label="Film A (1979)" id=65ab6056ca69 job=""')
    assert h.render({}, "logfmt") == 'arr="" source="" outcome=other class="" edits=0 reasons="" alerts="" tmdb=not_asked label="" id="" job=""'


def test_a_job_error_names_its_error_and_its_job_in_the_logfmt_line():
    """hd 2026-10-01: a store failure before the job read its file logged an error line with no app, label or path. The
    syslog line keeps its keys in their order, takes the job file as the label, and ends with the error."""
    rec = {"source": "hook", "job": "1790904088348071068-813305.json", "outcome": "error", "result": "error: OperationalError: disk I/O error"}
    assert h.render(rec, "logfmt") == ('arr="" source=hook outcome=error class="" edits=0 reasons="" alerts="" tmdb=not_asked '
                                       'label=1790904088348071068-813305.json id="" job=1790904088348071068-813305.json error="error: OperationalError: disk I/O error"')
    rec = dict(rec, path="/tv/Show/Season 1/Show - s01e01.mkv", result="error: " + "x" * 300)
    assert h.render(rec, "logfmt").endswith(f'label="Show - s01e01.mkv" id="" job=1790904088348071068-813305.json error="error: {"x" * 143}"')
    assert "error=" not in h.render(decision(), "logfmt")


def test_a_deep_analysis_line_names_the_job_of_its_import():
    """from= follows the old keys, before error, so a reader can match the line to the import that queued it."""
    rec = {"source": "deep_analysis", "job": "deep-analysis-0123456789abcdef.json", "from": "1790904088348071068-813305.json", "outcome": "error",
           "result": "error: OSError: x"}
    assert h.render(rec, "logfmt").endswith(' job=deep-analysis-0123456789abcdef.json from=1790904088348071068-813305.json error="error: OSError: x"')
    assert " from=" not in h.render(decision(), "logfmt")


def test_an_import_holds_only_the_subtitle_alerts_that_would_post(sent):
    """The deep analysis follows, so the import keeps each subtitle alert that would post in held, for that analysis.
    A subtitle fix that logs only stays log only, and every other alert posts at once."""
    late = {"kind": "subtiming", "lines": [{"code": "off", "track": "s1", "ref": None, "why": "x", "unfixed": 2.4}]}
    gone = {"kind": "submatch", "lines": [{"code": "removed", "track": "s3", "why": "x", "kept": "/k/F.mkv"}]}
    rec, held = decision(findings=[late, gone, DOUBT]), []
    assert h.alert_findings(rec, 7, held) == [h.HOLD_RESULT, "log only", "sent"] and [e["title"] for e in sent] == ["Audio may be broken"]
    assert [(x["kind"], x["size"], x["embed"]["title"]) for x in held] == [("subtiming", 7, "Subtitles out of sync")]
    assert h.alert_findings(rec, 7) == ["sent", "log only", "sent"]   # no deep analysis follows


def test_a_held_alert_counts_as_judged_only_when_each_subtitle_it_names_got_a_verdict():
    """A failed hearing says unknown, and a weak fit judges nothing. The speech layout judges a track no reference fits."""
    rec = {"subcheck": {"s1": {"verdict": "match"}, "s2": {"verdict": "unknown"}},
           "subtime": {"s3": {"verdict": "unknown", "layout": {"verdict": "fit"}}, "s4": {"verdict": "weak"}}}
    assert [h.judged(rec, k) for k in (["s1"], ["s1", "s2"], ["s3"], ["s4"], ["s5"], [])] == [True, False, True, False, False, False]
    rec = {"subremux": {"done": True, "timed": ["s1"], "removed": ["s3"]}, "sidecars": [{"name": "F.en.srt", "result": "retimed"},
                                                                                       {"name": "F.fr.srt", "result": "left"}]}
    assert h.remuxed(rec) == {"s1", "s3", "F.en.srt"} and h.remuxed({"subremux": {"done": False, "timed": ["s1"]}}) == set()


def test_a_conversion_a_held_alert_names_posts_once(sent, settings):
    """A sidecar that did not match the audio at the conversion to MKV stayed beside the file, and its held alert names
    the conversion. So the import posts no conversion change. When the deep analysis checks the sidecar again, the
    conversion posts as a change. When nothing checks it, the held alert posts and names the conversion itself."""
    settings(discord_posts="all")
    side = {"kind": "submatch", "lines": [{"code": "converted_sidecar", "name": "Film A.en.srt", "why": "x", "kept": None,
                                          "left": "the folder is read-only"}]}
    rec, held = decision(outcome="no_change", result="no change", edits=[], findings=[side], container="MP4/QuickTime",
                         repack={"new_size": 9, "kept": "/k/Film A.mp4"}), []
    assert h.alert_findings(rec, 7, held) == [h.HOLD_RESULT] and sent == [] and rec["change_result"] == []
    job = {"app": "radarr", "path": rec["path"], "held": held}
    assert h.held_after(job, {"alert_kinds": [], "subcheck": {"Film A.en.srt": {"verdict": "match"}}}) == ["checked again, change sent"]
    assert [e["title"] for e in sent] == ["Converted to MKV"]
    sent.clear()
    assert h.held_after(job, {"outcome": "error"}) == ["sent"] and [e["title"] for e in sent] == ["Wrong subtitles"]
    assert "MKV" in sent[0]["description"] or "convert" in sent[0]["description"].lower(), sent[0]["description"]


def test_held_keys_name_each_track_by_its_place_after_the_import_remux():
    """The import removed track 1, so the track that was 2 is track 1 when the deep analysis reads the file."""
    rec = decision(subremux={"done": True, "removed": ["s1"], "tracks_before": [{"i": "s1", "lang": "fre"}, {"i": "s2", "lang": "eng"}]})
    f = {"kind": "submatch", "lines": [{"code": "removed", "track": "s1", "why": "x", "kept": "/k/F.mkv"}, dict(STAYS, track="s2")]}
    assert h.named(rec, f) == ["s1"]


MIXED = {"kind": "submatch", "lines": [{"code": "removed", "track": "s1", "why": "x", "by": "hook", "kept": "/k/Film A.mkv"},
                                       {"code": "sidecar", "name": "Film A.en.srt", "why": "x", "kept": None, "left": "the folder is read-only"}]}


@pytest.mark.parametrize("posts", ["issues", "all"])
def test_a_held_alert_that_goes_unposted_still_posts_the_fix_of_the_import_in_all_mode(sent, settings, posts):
    """The import removed a track and left a sidecar that does not match. Their one alert waits for the deep analysis,
    which checks again and posts in its place. all mode still posts the removal, as the change the import made. issues
    mode posts nothing. When nothing checks again, the whole alert posts, and it names the removal itself."""
    settings(discord_posts=posts)
    rec, held = decision(outcome="no_change", result="no change", edits=[], findings=[MIXED], subremux={
        "done": True, "removed": ["s1"], "kept": "/k/Film A.mkv", "tracks_before": [{"i": "s1", "lang": "fre"}]}), []
    assert h.alert_findings(rec, 7, held) == [h.HOLD_RESULT] and sent == []
    job = {"app": "radarr", "path": rec["path"], "held": held}
    assert held[0]["keys"] == ["Film A.en.srt"]   # the removed track is settled, so only the sidecar waits for a verdict
    checked = {"alert_kinds": [], "outcome": "no_change", "subcheck": {"Film A.en.srt": {"verdict": "match"}}}
    assert h.held_after(job, checked) == ["checked again" + (", change sent" if posts == "all" else "")]
    assert described(sent) == ([("Wrong subtitles", "The **French subtitles (track 1 of the original file)** don't match what's said in the audio. "
                                                     "Removed them and kept the original file at /k/Film A.mkv.")] if posts == "all" else []), described(sent)
    sent.clear()
    assert h.held_after(job, {"outcome": "file_replaced"}) == ["dropped with the file" + (", change sent" if posts == "all" else "")]
    assert len(sent) == (posts == "all"), described(sent)
    sent.clear()
    assert h.held_after(job, {"outcome": "error"}) == ["sent"] and len(sent) == 1 and "Removed them" in sent[0]["description"], described(sent)


def test_the_embed_target_is_one_embed_per_finding():
    language, audio = h.render(decision(), "embed")
    assert {k: v for k, v in language.items() if k != "timestamp"} == {
        "title": "Wrong audio language", "description": "The audio is **Portuguese**, but it should be English.", "color": h.COLORS["amber"],
        "fields": [{"name": "Film A (1979)", "value": "Film A.mkv", "inline": False}], "footer": {"text": "arr-media-guard on host1"}}
    assert (audio["title"], audio["color"], audio["description"]) == (
        "Broken audio", h.COLORS["red"], "The audio is silent at all 3 places checked.\nThe limit of 10 re-grabs a day was reached, so the file was "
                                         "kept.")


# The goldens whose alert goes to the decision log only, by their place in FINDINGS, ACTIONS and SUB_LINES: a problem the
# program fixed. That is a re-grab, an old file put back that the app picked up, a removed subtitle, a moved sidecar, a
# track a conversion left out. Every other golden posts, a doubt and a failed restore too.
LOG_ONLY = {"findings": {27}, "actions": {0, 1, 2, 3, 4, 6, 7, 9}, "sub_lines": {0, 7, 10, 26, 27, 29}}
SUB_MATCH = ("removed", "stays", "sidecar", "converted_sidecar", "converted_track", "garbled", "repaired", "stripped")   # the sentences of a submatch finding


def every_alert():
    """(group, place, finding) of each golden: each finding, broken audio with each action, each subtitle sentence alone."""
    return ([("findings", i, f) for i, (f, *_) in enumerate(FINDINGS)]
            + [("actions", i, {"kind": "audio", "certain": SILENT, "action": a}) for i, (a, *_) in enumerate(ACTIONS)]
            + [("sub_lines", i, {"kind": "submatch" if x["code"] in SUB_MATCH else "subtiming", "lines": [x]}) for i, (x, *_) in enumerate(SUB_LINES)])


def post_all(monkeypatch):
    """alert_findings() of each golden in a record of its own, with each post faked. A re-grab decides for the other
    findings of its record, so each golden needs its own. Returns (what each finding gave, the titles posted)."""
    sent = []
    monkeypatch.setattr(h, "post", lambda app, emb: sent.append(emb["title"]) or "sent")
    monkeypatch.setattr(h.store, "add", lambda *a: True)   # no marker from an earlier post
    tracks = [{"i": k, "lang": v} for k, v in LANGS.items()]
    return [h.alert_findings(dict(decision(), tracks=tracks, findings=[f]), 1)[0] for *_, f in every_alert()], sent


def test_a_fixed_problem_logs_only_and_every_other_alert_posts(monkeypatch):
    """The shared gate, report.posts(), decides for every alert kind, action and subtitle sentence. The decision line keeps
    every finding, and its alert_result says "log only" for each one that did not post."""
    got, sent = post_all(monkeypatch)
    want = ["log only" if i in LOG_ONLY[g] else "sent" for g, i, _ in every_alert()]
    assert got == want, [(g, i, h.title(f)[0], r) for (g, i, f), r, w in zip(every_alert(), got, want) if r != w]
    assert sent == [h.title(f)[0] for (g, i, f), r in zip(every_alert(), got) if r == "sent"]


def test_the_shared_gate_is_the_only_gate(monkeypatch):
    """Mutation check: with report.posts() open every alert posts, and with it shut none does. So no other check in the
    shared path holds an alert back or lets one through."""
    monkeypatch.setattr(h, "posts", lambda f, rec=None: True)
    got, sent = post_all(monkeypatch)
    assert got == ["sent"] * len(every_alert()) and len(sent) == len(got)
    monkeypatch.setattr(h, "posts", lambda f, rec=None: False)
    got, sent = post_all(monkeypatch)
    assert got == ["log only"] * len(every_alert()) and sent == []


LENGTH = {"kind": "duration", "why": "The file says it runs 26:01, but the video and audio stop at 23:52. The runtime check was skipped."}
REGRABBED = {"code": "regrabbed", "name": "Sonarr", "kind": "video", "n": 1}
DOUBT = {"kind": "audio", "doubts": ["the audio fails to play at 1 of 3 places checked"]}
LANGUAGE, RUNTIME, EPISODE, CONTENT = (FINDINGS[i][0] for i in (0, 2, 4, 7))


# A run that made every kind of change: a conversion, a file repair, a subtitle remux that retimed two tracks and
# removed the first, and a track edit. Made-up facts, as process.py records them. The remux took out s1, so the posts
# name s2 as track 1 and s3 as track 2, the places of the file after the run.
CHANGED = dict(
    outcome="edited", result="edited", container="MP4/QuickTime",
    repack={"new_size": 9, "kept": "/k/Film A.mp4"}, header_repair={"code": "tail_removed", "removed": {}},
    subremux={"done": True, "fixed": ["s2"], "timed": ["s3"], "ended": ["s2"], "removed": ["s1"], "kept": "/k/Film A.mkv",
              "tracks_before": [{"i": "s1", "lang": "fre"}, {"i": "s2", "lang": "eng"}, {"i": "s3", "lang": "spa"}]},
    subcheck={"s2": {"verdict": "match", "timing": {"fix": {"offset": 2.5, "rate": "1/1"}}}},
    blocks={"s3": {"blocks": [{"cues": 12}, {"cues": 3}]}}, flash={"s2": {"lengthened": 40, "cues": 300, "median": 0.5}},
    edits=[["track:=1", 1, 0], ["track:=2", 0, 1], ["track:=3", 1, 0], ["track:=5", 0, 1, "flag-forced"], ["track:=1", "jpn", "und", "language"]],
    after=[{"sel": "track:=1", "pos": "a1", "lang": "jpn", "default": True}, {"sel": "track:=2", "pos": "a2", "lang": "eng", "default": False},
           {"sel": "track:=3", "pos": "s1", "lang": "eng", "default": True}, {"sel": "track:=5", "pos": "s2", "lang": "spa", "default": False}],
    findings=[{"kind": "submatch", "lines": [{"code": "removed", "track": "s1", "why": "x", "kept": "/k/Film A.mkv"}]}])
CHANGE_POSTS = [  # (title, color, description) of each post of CHANGED with DISCORD_POSTS all
    ("Wrong subtitles", "amber", "The **French subtitles (track 1 of the original file)** don't match what's said in the audio. Removed them and "
     "kept the original file at /k/Film A.mkv."),
    ("Converted to MKV", "green", "Converted the MP4/QuickTime file to MKV.\nIts video and audio stayed the same.\nThe original file is kept "
     "at /k/Film A.mp4."),
    ("File repaired", "green", "Removed extra data from the end of the file. Its video, audio and subtitles stayed the same."),
    ("Subtitles retimed", "green", "The **English subtitles (track 1)** were **about 2.5 s late**.\nRetimed them to match the speech.\nThe "
     "**English subtitles (track 1)** flashed by too fast to read.\nMade 40 of their lines stay on screen longer.\nMoved **15 lines** of the "
     "**Spanish subtitles (track 2)** to their speech.\nThe original file is kept at /k/Film A.mkv."),
    ("Default tracks, forced flag and language tag changed", "green", "Turned the default flag on for the **Japanese audio (track 1)** and "
     "the **English subtitles (track 1)**, and off for the **English audio (track 2)**.\nTurned the forced flag off for the **Spanish "
     "subtitles (track 2)**.\nTagged the **audio (track 1)** as **Japanese**.")]
REGRAB = dict(CONTENT, action={"code": "regrabbed", "name": "Radarr", "kind": "content", "n": 1})


def changed(**kw):
    """alert_findings() of the record CHANGED with kw, and the record."""
    rec = decision(**dict(CHANGED, **kw))
    return h.alert_findings(rec, 1), rec


def described(sent):
    return [(e["title"], e["description"]) for e in sent]


@pytest.fixture
def sent(monkeypatch):
    """The embeds that post() would send. No marker of an earlier post stops one."""
    out = []
    monkeypatch.setattr(h, "post", lambda app, emb: out.append(emb) or "sent")
    monkeypatch.setattr(h.store, "add", lambda *a: True)
    return out


def test_discord_posts_issues_posts_nothing_for_a_change(sent):
    """The default, issues: a run that changed its file in every way and fixed its problems posts nothing."""
    assert h.CFG.discord_posts == "issues"
    got, rec = changed()
    assert got == ["log only"] and sent == [] and "change_result" not in rec


def test_discord_posts_all_posts_one_per_change_and_nothing_for_no_change(sent, settings):
    """all: one post for each change, in the words of the issue posts. A fix that the issue gate logs only keeps its
    alert. A change no finding words posts in green. Every post names a track by its place in the file after the run.
    A clean check, a dry run and an issue alone post no change."""
    settings(discord_posts="all")
    got, rec = changed()
    assert got == ["log only"] and rec["change_result"] == ["sent"] * len(CHANGE_POSTS)
    assert [(e["title"], e["color"], e["description"]) for e in sent] == [(t, h.COLORS[c], d) for t, c, d in CHANGE_POSTS]
    assert all(e["fields"] == [{"name": "Film A (1979)", "value": "Film A.mkv", "inline": False}] for e in sent)
    assert h.render(decision(**CHANGED), "log")["alerts"][0].startswith("submatch: The French subtitles (track 1) don't match")   # the log keeps the places the check saw
    sent.clear()
    clean = decision(outcome="no_change", result="no change", edits=[], findings=[DOUBT])
    assert h.alert_findings(clean, 1) == ["sent"] and clean["change_result"] == [] and [e["title"] for e in sent] == ["Audio may be broken"]
    sent.clear()
    dry = decision(**dict(CHANGED, apply=False, outcome="dry_run", result="dry run", repack={}, header_repair={"code": "would_repair_header"},
                          subremux=dict(CHANGED["subremux"], done=False, removed=[]), findings=[]))
    assert h.alert_findings(dry, 1) == [] and dry["change_result"] == [] and sent == []


def test_after_a_re_grab_only_the_re_grab_posts(sent, settings):
    """The re-grab deleted the file, so its other changes are gone too. issues mode logs the re-grab only."""
    settings(discord_posts="all")
    got, rec = changed(outcome="wrong_content", result="wrong content: x", edit_result="edited", findings=[REGRAB, LANGUAGE, *CHANGED["findings"]])
    assert got == ["log only"] * 3 and [e["title"] for e in sent] == ["Wrong content, re-grabbed"] and rec["change_result"] == ["sent"]
    sent.clear()
    got, rec = changed(findings=[dict(REGRAB, action={"code": "deleted"})])   # the issue alert says it, and no change posts
    assert got == ["sent"] and rec["change_result"] == [] and [e["title"] for e in sent] == ["Wrong content"]


LIVE = {"code": "live", "track": "s3", "lag": 4.0, "moved": 900, "cues": 1200, "left": 300, "flags_off": True}


@pytest.mark.parametrize("kw, want", [
    ({"subremux": dict(CHANGED["subremux"], fixed=[], ended=[]), "blocks": {"s3": {"blocks": [{"cues": 900}], "live": {"moved": 900}}},
      "edits": [], "findings": [{"kind": "subtiming", "lines": [LIVE]}]}, ["Subtitles out of sync"]),
    ({"subremux": {}, "edits": [["track:=5", 0, 1], ["track:=5", 0, 1, "flag-forced"]],
      "findings": [{"kind": "submatch", "lines": [dict(STAYS, track="s2")]}]}, ["Wrong subtitles"]),
    ({"subremux": {}, "edits": [["track:=5", 0, 1], ["track:=1", 1, 0]],
      "findings": [{"kind": "sublang", "mismatch": ["subtitle track 2 is tagged Spanish, but its text reads as Romanian"],
                    "muted": ["s2 loses its default and forced flags, because x"]}]}, ["Subtitle language may be wrong", "Default tracks changed"]),
    ({"subremux": {}, "edits": [], "repack": CHANGED["repack"],
      "findings": [{"kind": "submatch", "lines": [{"code": "converted_track", "track": "s2", "why": "x", "kept": "/k/F.avi"}]}]}, ["Wrong subtitles"])])
def test_a_fix_an_alert_says_posts_once(sent, settings, kw, want):
    """A live caption alert says which lines moved, a stays or a language alert says which flags went off, and a track
    a conversion left out names the conversion. Their change posts would say it twice."""
    settings(discord_posts="all")
    changed(**dict(dict(repack={}, header_repair={}), **kw))
    assert [e["title"] for e in sent] == want, described(sent)


def test_a_partial_shift_says_how_many_lines_kept_their_times(sent, settings):
    """A fix of the speech layout that moves only the lines that agree with it, see subsync.layout_fix()."""
    rec = decision(edits=[], findings=[], subremux={"done": True, "fixed": ["s3"], "kept": "/k/F.mkv", "tracks_before": [{"i": "s3", "lang": "fre"}]},
                   subtime={"s3": {"verdict": "fit", "timing": {"fix": {"offset": 4.2, "rate": "1/1"}, "keep": [[0.0, 50.0, 6]], "kept": 6}}})
    assert [(t, h.markdown(x)) for t, x in h.changes(rec)] == [("Subtitles retimed", (
        "The **French subtitles (track 3)** were **about 4.2 s late**.\nRetimed them to match the speech.\nKept the times of 6 lines at the start "
        "or end.\nThe original file is kept at /k/F.mkv."))]


@pytest.mark.parametrize("edits, after, want", [
    ([["track:=2", "spa", "eng", "language"], ["track:=2", "spa", "en-US", "language-ietf"]], "spa",
     ("Language tag changed", "Changed the language of the **audio (track 1)** from English to **Spanish**.")),
    ([["track:=2", "en-US", "eng", "language"]], "eng", ("Language tag changed", "Wrote the language tag of the **English audio (track 1)** in its "
                                                                             "standard form.")),
    ([["track:=2", 0, 1, "flag-forced"], ["track:=4", 0, 1, "flag-forced"]], "eng",
     ("Forced flags changed", "Turned the forced flag off for the **English audio (track 1)** and the **English subtitles (track 1)**."))])
def test_a_track_edit_says_each_kind_it_made_and_only_those(edits, after, want):
    """A language tag edit alone is no change of the default tracks. The title follows the kinds of edit."""
    rec = decision(edits=edits, findings=[], after=[{"sel": "track:=2", "pos": "a1", "lang": after, "default": True},
                                                    {"sel": "track:=4", "pos": "s1", "lang": "eng", "default": False}])
    assert [(t, h.markdown(x)) for t, x in h.changes(rec)] == [want]


def test_a_repair_that_removes_and_trims_says_both():
    rec = decision(edits=[], findings=[], header_repair={"code": "subtitle_removed", "removed": {"4": {}}, "trimmed": {"3": 4}})
    assert h.changes(rec) == [("File repaired", "Removed the subtitles that kept going past the end of the video and audio. Cut the subtitle "
                                                "lines that kept going past the end of the video and audio.")]


def test_a_change_post_says_no_internal_word_and_gives_no_advice(sent, settings):
    """The change posts follow the voice of the issue posts. A broken fact costs only its own change: it posts nothing,
    its line in change_result says why, and the other changes still post. It never costs the decision line."""
    settings(discord_posts="all")
    said = [h.unmarked(t + " " + x) for t, x in h.changes(decision(**CHANGED))] + [h.unmarked(t + " " + x) for t, x in h.REPAIRS.items()]
    assert not [(s, INTERNAL.findall(s)) for s in said if INTERNAL.search(s)] and not [s for s in said if INSTRUCTION.search(s)]
    got, rec = changed(blocks={"s3": {"blocks": [{}]}})
    assert rec["change_result"] == ["sent"] * 3 + ["no text: Subtitles retimed: KeyError: 'cues'", "sent"]
    assert described(sent) == [(t, d) for t, c, d in CHANGE_POSTS if t != "Subtitles retimed"]
    sent.clear()
    got, rec = changed(findings=[{"kind": "submatch", "lines": [{"code": "removed", "track": "s1", "why": "x"}]}])   # no kept
    assert rec["change_result"][0] == "no text: KeyError: 'kept'" and "Wrong subtitles" not in [e["title"] for e in sent] and len(sent) == 4


def test_an_alert_whose_text_fails_posts_a_plain_sentence(sent):
    """Owner 2026-10-05: the problem still posts, with its title and its file, and the decision line keeps the error."""
    rec = decision(findings=[{"kind": "runtime", "listed": 62}])   # no runs
    assert h.alert_findings(rec, 1) == ["sent"]
    (e,) = sent
    assert (e["title"], e["description"], e["fields"]) == ("Wrong runtime", h.UNTOLD, [{"name": "Film A (1979)", "value": "Film A.mkv", "inline": False}])
    assert "AMG" in e["description"] and not re.search(r"\w+(Error|Exception)\b|no text", e["description"])
    assert h.render(rec, "log")["alerts"] == ["runtime: no text: KeyError: 'runs'"]


@pytest.mark.parametrize("tries, wait, want, calls_n, sleeps_n", [
    (3, 0.5, "sent", 4, 3), (4, 0.5, "failed: Discord asked for no posts for 0.5 s", 4, 3),
    (1, 3600, "failed: Discord asked for no posts for 3600 s", 1, 0)])
def test_a_post_waits_out_each_429_up_to_its_tries(monkeypatch, settings, tries, wait, want, calls_n, sleeps_n):
    """Several job processes may post at once, as for a season pack. Each 429 waits Discord's retry_after. A wait over
    POST_WAIT, or the last try, gives up, and the later posts of the process skip with no request until that time."""
    settings(discord_webhook=HOOK)
    calls, sleeps, clock = [], [], [100.0]
    def http(url, method="GET", body=None, headers=None, timeout=15):
        calls.append(url)
        if len(calls) <= tries:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(json.dumps({"retry_after": wait}).encode()))
    monkeypatch.setattr(h, "http", http)
    monkeypatch.setattr(h, "QUIET_UNTIL", 0.0)
    monkeypatch.setattr(h.time, "sleep", sleeps.append)
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    assert h.post("radarr", {"title": "x"}) == want and len(calls) == calls_n and sleeps == [wait] * sleeps_n
    if want == "sent":
        return
    assert h.post("radarr", {"title": "y"}) == f"skipped, Discord asked for no posts for {wait:.0f} s more" and len(calls) == calls_n
    clock[0] += wait
    assert h.post("radarr", {"title": "z"}) == "sent" and len(calls) == calls_n + 1


def test_an_overrun_alert_after_a_strip_names_the_track_that_runs_over():
    """The remux took out the garbled French track s1, and the English PGS track that runs past the end moved up to s1.
    faults() maps it back past both a removal and a strip, so the alert names the English track by its place after the
    run."""
    rec = decision(edits=[], findings=[], header_repair={"code": "subtitle_overrun_unfixable"},
                   subremux={"done": True, "removed": [], "stripped": {"s1": "Film A.fre.srt"}, "kept": "/k/A.mkv",
                             "tracks_before": [{"i": "s1", "lang": "fre"}, {"i": "s2", "lang": "eng"}]})
    hp = {"issue": ["a subtitle runs past the end"], "unfixable": [{"track": "s1", "codec": "S_HDMV/PGS", "end": 7300.0, "streams": 6000.0}]}
    ctx = types.SimpleNamespace(rec=rec, d={}, hp=hp, vpre=None, damaged=None, unconverted=None, tags=None, mode="sub_time")
    h.faults(ctx)
    assert ctx.rest[0]["tracks"][0]["track"] == "s2"
    assert h.render(dict(rec, findings=ctx.rest), "embed")[0]["description"].startswith("The **English subtitles (track 1)** keep going until")


@pytest.mark.parametrize("answer, ok, line", [
    (204, True, "Discord took the test message: HTTP 204."),
    (urllib.error.HTTPError(HOOK, 404, "Not Found", {}, io.BytesIO(b'{"message": "Unknown Webhook", "code": 10015}')), False,
     "Discord refused the test message: HTTP 404 Not Found. Unknown Webhook (code 10015)."),
    (urllib.error.HTTPError(HOOK, 404, "Not Found", {}, io.BytesIO(b"Cannot POST /api/webhooks/1/t0ken-0123456789")), False,
     "Discord refused the test message: HTTP 404 Not Found."),
    (ValueError(f"unknown url type: {HOOK}"), False, "The test message failed: ValueError: unknown url type: <DISCORD_WEBHOOK>"),
    (ValueError(f"URL can't contain control characters. {'https://discord.example/api/webhooks/2/t0ken-x' + chr(10)!r}"), False,
     "The test message failed: ValueError: URL can't contain control characters. 'https://discord.example/api/webhooks/<hidden>'"),
    (None, False, "DISCORD_WEBHOOK is not set, so no test message was sent.")])
def test_the_discord_test_posts_one_message_and_prints_the_answer(monkeypatch, settings, capsys, answer, ok, line):
    """--test-discord posts one message and prints Discord's HTTP status. A refusal or an error prints the reason with the
    webhook masked and exits 1. A body that is not Discord's JSON may echo the path, so it is left out. With no webhook
    it posts nothing and exits 1."""
    settings(discord_webhook="" if answer is None else HOOK)
    calls = []

    def urlopen(req, timeout):
        calls.append(json.loads(req.data))
        if isinstance(answer, Exception):
            raise answer
        return contextlib.nullcontext(types.SimpleNamespace(status=answer))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    if ok:
        h.main(["--test-discord"])
        assert capsys.readouterr().out == line + "\n"
    else:
        with pytest.raises(SystemExit) as ex:
            h.main(["--test-discord"])
        assert str(ex.value) == line and ex.value.code != 0 and "t0ken" not in str(ex.value)
    assert calls == ([] if answer is None else [{"username": "arr-media-guard", "content": "Test message from arr-media-guard on host1.",
                                                 "allowed_mentions": {"parse": []}}])


@pytest.mark.parametrize("findings, want", [
    ([{"kind": "video", "certain": "x", "action": REGRABBED}, DOUBT], ["log only", "log only"]),   # hd, Outlander S05E08
    ([{"kind": "video", "certain": "x", "action": dict(REGRABBED, code="deleted")}, DOUBT, LANGUAGE], ["sent", "log only", "log only"]),
    ([dict(CONTENT, action={"code": "regrabbed", "name": "Radarr", "kind": "content", "n": 1}), LANGUAGE, RUNTIME, LENGTH],
     ["log only"] * 4),
    ([dict(CONTENT, action={"code": "would_regrab", "kind": "content"}, scored=["language", "runtime"]), LANGUAGE, RUNTIME, EPISODE, LENGTH, DOUBT],
     ["sent", "log only", "log only", "sent", "sent", "sent"]),
    ([dict(CONTENT, action={"code": "would_regrab", "kind": "content"}), EPISODE], ["sent", "sent"]),   # the episode signal scores 0 points
    ([dict(CONTENT, signals=["the release name says 2017, but the listed year is 1979", "it runs 10 minutes, but the listed runtime is 62 minutes"],
           scored=["year", "runtime"]), LANGUAGE], ["sent", "sent"]),                              # TMDB couldn't tell, the tracks found it
    ([CONTENT, LANGUAGE], ["sent", "log only"]),                                                   # a backfill finds it, nothing re-grabs
    ([{"kind": "audio", "certain": SILENT, "action": dict(ACTIONS[19][0])}, DOUBT], ["sent", "log only"]),   # no old file of its own came back
    ([{"kind": "audio", "certain": SILENT, "action": dict(ACTIONS[5][0])}], ["sent"]),            # the app didn't pick the old files up
    ([{"kind": "audio", "certain": SILENT, "action": dict(ACTIONS[9][0])}, DOUBT], ["log only", "log only"]),
])
def test_a_fix_that_worked_logs_every_finding_of_its_file(monkeypatch, findings, want):
    """One cause posts once. A re-grab or a restore that worked logs only, and so does every other finding of the file
    it deleted. A restore that put back no old file of its own, or old files the app did not pick up, posts. A
    wrong-content alert names each signal that scored. A language or runtime finding logs only beside it when it names
    that signal. The episode signal scores no points, so the episode finding posts. A language finding that only the
    track decision found posts too."""
    monkeypatch.setattr(h, "post", lambda app, emb: "sent")
    monkeypatch.setattr(h.store, "add", lambda *a: True)
    assert h.alert_findings(decision(findings=findings), 1) == want


@pytest.mark.parametrize("code, others, want", [
    ("header_repaired", [], ["log only"]),                                                   # the repair fixed it
    ("header_repair_failed", [{"kind": "header", "why": "x"}], ["log only", "sent"]),        # "File repair failed" names the cause
    ("subtitle_file_may_be_cut", [{"kind": "cut", "why": "x"}], ["log only", "sent"]),
    ("subtitle_overrun_unfixable", [{"kind": "subtitle", "issue": [], "tracks": []}], ["log only", "sent"]),
    (None, [], ["sent"]),                                                                    # HEADER_REPAIR off: no repair ran
    ("header_not_repaired", [], ["sent"]),                                                   # a fault blocked the repair
])
def test_a_wrong_length_posts_once_and_only_when_no_repair_ran(monkeypatch, code, others, want):
    """One cause posts once. A wrong length in the file logs only when the repair fixed it, or when the alert of a failed
    repair, a cut file or a subtitle past the end already names its cause."""
    monkeypatch.setattr(h, "post", lambda app, emb: "sent")
    monkeypatch.setattr(h.store, "add", lambda *a: True)
    rec = decision(findings=[LENGTH, *others], **({"header_repair": {"code": code}} if code else {}))
    assert h.alert_findings(rec, 1) == want


def test_an_embed_bolds_the_names_and_escapes_their_markdown():
    """The embed bolds the episode titles and the track names. A markdown character in a name is escaped, so it never
    breaks the bold. The decision line and the CLI stay plain."""
    f = {"kind": "episode", "imported": [["S01E02", "Over*night_"]], "said": "the release name", "title": "Anxious ~Times~ at `Show` | Alpha\\",
         "names": "S01E03"}
    (e,) = h.render(decision(findings=[f]), "embed")
    assert e["description"] == ('Imported as S01E02 **"Over\\*night\\_"**. The release name calls it **"Anxious \\~Times\\~ at \\`Show\\` '
                                '\\| Alpha\\\\"**, which is S01E03.')
    assert h.render(decision(findings=[f]), "log")["alerts"] == ['episode: Imported as S01E02 "Over*night_". The release name calls it '
                                                                 '"Anxious ~Times~ at `Show` | Alpha\\", which is S01E03.']
    lines = [{"code": "check_times", "track": "s1", "why": "x", "fix": {"offset": 139.06, "rate": "1/1"}}, {"code": "removed", "track": "s2",
             "why": "x", "kept": "/k/F_1.mkv"}]
    (e,) = h.render(decision(findings=[{"kind": "subtiming", "lines": lines}], tracks=[{"i": "s1", "lang": "eng"}, {"i": "s2", "lang": "spa"}]), "embed")
    assert e["description"] == ("The **English subtitles (track 1)** are **about 2 min 19 s late**.\nSUBTITLES is set to check, so they were left as "
                                "they are.\nThe **Spanish subtitles (track 2)** don't match what's said in the audio.\nRemoved them and kept the "
                                "original file at /k/F\\_1.mkv.")


def test_an_embed_breaks_its_lines_only_at_the_sentence_ends_of_its_template():
    """A library record: the file name in an edit error holds "KS Rover Vs. Moped", and the label of another show holds
    markdown. The embed breaks no line inside a fact, and escapes the markdown of the field too. The decision line
    keeps its plain text."""
    f = {"kind": "edit", "error": "mkvpropedit failed: Error: The file 'KS Rover Vs. Moped.mkv' is not a Matroska file.", "unread": None,
         "on": ["a1 eng"]}
    rec = decision(findings=[f], label="M*A*S*H S01E01", path="/tv/M_A_S_H.mkv")
    (e,) = h.render(rec, "embed")
    assert e["description"] == ("Couldn't change which tracks play by default.\nMkvpropedit failed: Error: The file 'KS Rover Vs. Moped.mkv' is "
                                "not a Matroska file.\nThe file still opens, and its default tracks are the **English audio (track 1)**.")
    assert e["fields"] == [{"name": "M\\*A\\*S\\*H S01E01", "value": "M\\_A\\_S\\_H.mkv", "inline": False}]
    assert h.render(rec, "log")["alerts"] == ["edit: Couldn't change which tracks play by default. Mkvpropedit failed: Error: The file 'KS Rover "
                                              "Vs. Moped.mkv' is not a Matroska file. The file still opens, and its default tracks are the "
                                              "English audio (track 1)."]


SHOW_ALPHA = {"kind": "episode", "imported": [["S01E02", "Overnight"]], "said": "the release name", "title": "Anxious Times at Show Alpha",
              "names": "S01E03"}
ALPHA_FILE = "Show Alpha (2023) - s01e02 - Overnight - WEBDL-720p.mkv"
TV, MOVIES = "https://tv.media-host.test", "https://movies.media-host.test"   # made-up bases, an https proxy per app


def show_alpha(**kw):
    return decision(app="sonarr", label="Show Alpha (2023) S01E02", path=f"/media/{ALPHA_FILE}", ids={"slug": "show-alpha-2023"},
                    findings=[SHOW_ALPHA], **kw)


PLAIN_FIELD = [{"name": "Show Alpha (2023) S01E02", "value": ALPHA_FILE, "inline": False}]   # the field with no link


@pytest.mark.parametrize("app, label, slug, finding, base, url", [
    ("sonarr", "Show Alpha (2023) S01E02", "show-alpha-2023", SHOW_ALPHA, TV, f"{TV}/series/show-alpha-2023"),
    ("radarr", "Film A (1979)", "90001", {"kind": "language", "want": "English", "has": ["por"]}, MOVIES + "/", f"{MOVIES}/movie/90001")])
def test_an_alert_links_the_item_to_its_page_in_the_app(settings, app, label, slug, finding, base, url):
    """The owner asked for a link to the item in its app. The field shows the item's name as a link above the file,
    with a blank name, because Discord shows no link in a field name. The decision line, the logfmt line and the CLI
    line stay plain."""
    settings(**{app: {"link": base}})
    rec = decision(app=app, label=label, path=f"/media/{ALPHA_FILE}", ids={"slug": slug}, findings=[finding])
    (e,) = h.render(rec, "embed")
    assert e["fields"] == [{"name": "​", "value": f"[{label}]({url})\n{ALPHA_FILE}", "inline": False}]
    plain = json.dumps(h.render(rec, "log")["alerts"]) + h.render(rec, "logfmt") + h.render(rec, "cli")
    assert "](" not in plain and "http" not in plain


def test_an_empty_link_setting_gives_no_link_whatever_the_app_url(settings):
    """Owner: the connection URL may not be the address a browser opens, as behind a reverse proxy or in Docker. So
    only <KEY>_LINK makes a link."""
    settings(sonarr={"url": TV, "link": ""})
    assert h.render(show_alpha(), "embed")[0]["fields"] == PLAIN_FIELD


@pytest.mark.parametrize("base, url", [
    ("http://admin:pa55-0123456789@sonarr.lan:8989/base/?apikey=k3y-0123456789#top", "http://sonarr.lan:8989/base/series/show-alpha-2023"),
    ("http://[FE80::1]:8989", "http://[fe80::1]:8989/series/show-alpha-2023"),
    ("https://TV.media-host.test/a b(1)", "https://tv.media-host.test/a%20b%281%29/series/show-alpha-2023"),
    ("sonarr.lan:8989", None), ("ftp://sonarr.lan/", None), ("http://[sonarr", None), ("http://admin@:8989", None),
    ("http://host:99999/", None), ("http://ho)st/", None), ("http://ho st/", None),
    ("http://admin:8989/pa55@sonarr.lan/", None), ("http://admin:pa#55@sonarr.lan/", None), ("http://admin:pa?55@sonarr.lan/", None)])
def test_a_link_keeps_only_a_clean_host_port_and_path(settings, base, url):
    """The link never holds a user, a password, a query or a fragment. A password with a "/", "#" or "?" moves the "@"
    out of the host part, and such a base gives no link. A host with other characters gives no link. A base that gives no
    link leaves the field as before, and the alert still renders."""
    settings(sonarr={"link": base})
    (e,) = h.render(show_alpha(), "embed")
    assert e["fields"] == ([{"name": "​", "value": f"[Show Alpha (2023) S01E02]({url})\n{ALPHA_FILE}", "inline": False}] if url else PLAIN_FIELD)
    assert not [x for x in ("admin", "pa55", "k3y", "apikey", "top") if x in json.dumps(e)]


def test_an_item_with_no_slug_alerts_as_before(monkeypatch, settings):
    """An error record, or an item whose app lookup failed, has no slug. Its alert has no link and still posts."""
    settings(radarr={"link": MOVIES})
    sent = []
    monkeypatch.setattr(h, "post", lambda app, emb: sent.append(emb) or "sent")
    monkeypatch.setattr(h.store, "add", lambda *a: True)
    for ids in (None, {}, {"slug": None}, {"slug": ""}):
        rec = decision(**({} if ids is None else {"ids": ids}), findings=[{"kind": "language", "want": "English", "has": ["por"]}])
        assert h.alert_findings(rec, 1) == ["sent"]
        assert sent.pop()["fields"] == [{"name": "Film A (1979)", "value": "Film A.mkv", "inline": False}]


def test_a_title_with_markdown_and_brackets_keeps_its_link_whole(settings):
    """A ")" or "]" in a title never ends the link early, and a "*" never starts a bold span. The slug is percent-encoded.
    A title whose brackets do not pair gets no link, because Discord would end the link at the lone bracket."""
    settings(radarr={"link": MOVIES})
    label = "Who Framed *Roger* [Rabbit] (1988) :)"
    e = h.render(decision(label=label, path="/m/Roger_Rabbit.mkv", ids={"slug": "roger (1988) [x]/y"}), "embed")[0]
    assert e["fields"] == [{"name": "​", "value": f"[Who Framed \\*Roger\\* \\[Rabbit\\] (1988) :)]({MOVIES}/movie/"
                                                       "roger%20%281988%29%20%5Bx%5D%2Fy)\nRoger\\_Rabbit.mkv", "inline": False}]
    for label in ("Foo [ Bar (2001)", "Foo ] Bar [ (2001)", "Foo [[ Bar ] (2001)"):
        e = h.render(decision(label=label, path="/m/Foo.mkv", ids={"slug": "1"}), "embed")[0]
        assert e["fields"] == [{"name": label, "value": "Foo.mkv", "inline": False}]
    assert h.markdown(f'Two. Lines. {h.link("Mr. Robot S01E01", "http://h/series/mr-robot")}: OK') == \
        "Two.\nLines.\n[Mr. Robot S01E01](http://h/series/mr-robot): OK"   # a link span never breaks at a full stop


def test_an_edit_error_keeps_its_reason_after_a_long_path():
    """A library record: the path pushed the reason of mkvpropedit past the cut at 150 characters. The path becomes the
    file's name, cut as far as needed."""
    path = "/mnt/media/tv/Kid Show/Season 3/Kid Show - s03e51-e52 - Tower of the Glens + KS Rover Vs. Moped - WEBDL-1080p.mkv"
    got = h.short_error(f"mkvpropedit failed: Error: The file '{path}' is not a Matroska file or it could not be found.", path)
    assert got == ("mkvpropedit failed: Error: The file 'Kid Show - s03e51-e52 - Tower of the Glens + KS Rover Vs. Mope…' is not a Matroska "
                   "file or it could not be found.") and len(got) == 150
    assert h.short_error("mkvpropedit failed: x", path) == "mkvpropedit failed: x" and h.short_error("y" * 200, path) == "y" * 150


def test_a_tmdb_failure_shows_in_the_footer_of_a_language_alert_only():
    """The footer names TMDB only when its language check did not run, and only on the alerts that check bears on."""
    language, audio = h.render(decision(tmdb="tmdb_unavailable"), "embed")
    assert language["footer"]["text"] == "TMDB didn't answer, so its language check was skipped · arr-media-guard on host1"
    assert audio["footer"]["text"] == "arr-media-guard on host1"
    assert h.render(decision(tmdb="no_record"), "embed")[0]["footer"]["text"].startswith("TMDB has no record of this item, so")


def test_a_subtitle_alert_names_each_track_by_its_language_before_any_remux():
    """A removal moves the later tracks up one place. The sentences name the places the check saw, so the languages come
    from the tracks before the remux."""
    lines = [{"code": "removed", "track": "s1", "why": "x", "by": "hook", "kept": "/k/F.mkv"}]
    rec = decision(findings=[{"kind": "submatch", "lines": lines}], tracks=[{"i": "s1", "lang": "eng"}],
                   subremux={"tracks_before": [{"i": "s1", "lang": "spa"}, {"i": "s2", "lang": "eng"}]})
    assert h.render(rec, "embed")[0]["description"].startswith("The **Spanish subtitles (track 1)** don't match")
    assert h.render(dict(rec, subremux={}), "log")["alerts"][0].startswith("submatch: The English subtitles (track 1) don't match")


# A sentence that tells the viewer what to do. The alerts say what is wrong and what the program did.
INSTRUCTION = re.compile(r"(^|[.!?] )(Check|Fix|Set|Add|Replace|Raise|Rescan|Play|Import|Pick|Free|Make|Create|Run|Delete|Move)\b|\bby hand\b")
# The words a viewer never needs: the program's own names for its steps, and the file's internals
INTERNAL = re.compile(r"\b(hook|sweep|fitted line|header|container|events?|cues?|S_TEXT/\w+|SubRip|Matroska|[as]\d+|remux\w*|proof|points?|"
                      r"hunter|keep link)\b|--force", re.I)


def hunter_posts():
    """The titles and sentences of the subtitle hunter's posts, read from arr_subhunt.py: each text that replace() and
    restore() return, and each title and text that hunt() posts. A placeholder reads as {}, and a part of an f-string
    counts in its whole."""
    tree = ast.parse(open(os.path.join(ROOT, "arr_subhunt.py")).read())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    text = lambda n: "".join(x.value if isinstance(x, ast.Constant) else "{}" for x in n.values) if isinstance(n, ast.JoinedStr) else n.value
    words = lambda n: [text(x) for x in ast.walk(n) if isinstance(x, ast.JoinedStr) or isinstance(x, ast.Constant) and isinstance(x.value, str)]
    out = []
    for name in ("replace", "restore", "hunt"):
        for n in ast.walk(funcs[name]):
            if isinstance(n, ast.Return) and name != "hunt" or isinstance(n, (ast.Assign, ast.AugAssign)) and any(
                    getattr(t, "id", None) in ("text", "unsure", "err") for t in getattr(n, "targets", [getattr(n, "target", None)])):
                out += words(n.value)
            elif isinstance(n, ast.Call) and (getattr(n.func, "id", None) == "post" or getattr(n.func, "attr", None) == "embed"):
                out += words(n.args[0 if getattr(n.func, "id", None) == "post" else 1])
    return [x for x in dict.fromkeys(out) if " " in x and not any(x != y and x in y for y in out)]


def test_no_alert_says_an_internal_word_or_tells_the_viewer_what_to_do():
    """Every Discord alert kind, title and text in the done tense, with every action and every subtitle sentence, says
    what is wrong in a viewer's words, and what the program did. So does every post of the subtitle hunter. A tool's
    error text passes through as it is."""
    hunter = hunter_posts()
    assert len(hunter) >= 20 and "Old file left by an earlier run" in hunter, hunter
    said = hunter + [" ".join([h.title(f)[0], *(x for x in h.texts(f, "done", LANGS) if x)]) for f, *_ in FINDINGS]
    said += [" ".join([h.title(dict(f, action=a))[0], h.texts(dict(f, action=a), "done")[1]]) for f in [{"kind": "audio", "certain": SILENT}]
             for a, *_ in ACTIONS]
    said += [h.unmarked(h.sub_line(x, "done", LANGS)) for x, *_ in SUB_LINES] + [h.unmarked(h.sub_line(x, "done")) for x, *_ in SUB_LINES]
    assert {f["kind"] for f, *_ in FINDINGS} == set(h.FINDINGS) and not [(s, INTERNAL.findall(s)) for s in said if INTERNAL.search(s)]
    assert not [s for s in said if INSTRUCTION.search(s)]


def test_the_cli_target_is_the_backfill_line():
    rec = decision(apply=False, result="dry run", before=[{"sel": "track:=2", "pos": "a1"}], edits=[["track:=2", 0, 1], ["track:=3", 1, 0, "flag-forced"]],
                   heard={"a1": {"lang": "eng"}, "a2": {"lang": None}}, notes=["a1 is the original"], findings=[{"kind": "language", "want": "English",
                                                                                                                  "has": ["por"]}])
    assert h.render(rec, "cli") == ("dry run      Film A (1979) | a1 1->0, track:=3 flag-forced 0->1 | heard a1 eng, a2 no answer | a1 is the "
                                    "original | ALERT language: The audio is Portuguese, but it should be English.")
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
    rec = decision(apply=False, findings=[{"kind": "submatch", "lines": [line]}], alert_kinds=["submatch"], tracks=[{"i": "s1", "lang": "eng"}])
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
