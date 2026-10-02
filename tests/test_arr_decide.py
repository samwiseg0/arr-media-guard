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
"""Unit tests for decide.py, the flag decision rules. They run on TEST_POLICY, a fixed copy of the example policy, so
a change to a policy file never fails them.

Run: pytest tests/test_arr_decide.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from arr_media_guard import decide  # noqa: E402

TEST_POLICY = {"kids": {"genres": {"radarr": ["Family"], "sonarr": ["Children", "Family"]}, "profiles": ["Kids"],
                        "studios": ["Studio A"]},
               "audio": {"english": ["original"], "foreign": ["original", "english"], "foreign_kids": ["english", "original"]},
               "subtitles": {"english": ["forced"], "foreign": ["full", "sdh", "forced", "dub"]},
               "sparse_events": 1.5, "forced_flag_events": 4.0, "density_min_minutes": 15, "min_confidence": 0.7,
               "forced_clear": {"events": 10.0, "english_only_audio": True, "reference_ratio": 0.8}}


def test_the_decision_rules(monkeypatch):
    monkeypatch.setattr(decide, "POLICY", decide.POLICY)   # the live policy comes back after the test
    decide.set_policy(TEST_POLICY)

    def ff(*streams):   # ffprobe shape: (type, lang, title, channels, default, forced, commentary)
        return {"streams": [{"codec_type": c, "channels": ch, "tags": {"language": l, "title": ti},
                             "disposition": {"default": d, "forced": fo, "comment": co}} for c, l, ti, ch, d, fo, co in streams]}

    def mk(typ, lang, name, dflt, forced=False, uid=None, **kw):   # mkvmerge -J track
        return {"type": typ, "properties": dict(language=lang, track_name=name, default_track=dflt, forced_track=forced, uid=uid, **kw)}

    # English film, Portuguese default audio and a Portuguese default subtitle
    d = decide.decide(ff(("video", None, None, 0, 1, 0, 0), ("audio", "por", "Brazilian Portuguese", 6, 1, 0, 0), ("audio", "eng", "English", 6, 0, 0, 0),
                  ("subtitle", "por", "", 0, 1, 0, 0), ("subtitle", "eng", "SDH", 0, 0, 0, 0)), "English")
    assert d["edits"] == [["track:a1", 0, 1], ["track:a2", 1, 0], ["track:s1", 0, 1]] and not d["undecided"], d
    assert decide.plan_class(d) == "English original: audio switched, foreign subtitle off", decide.plan_class(d)
    # English film: a forced English subtitle stays default, by flag or by title. A full English one and a forced Portuguese one are cleared.
    d = decide.decide(ff(("audio", "eng", "", 6, 1, 0, 0), ("subtitle", "eng", "", 0, 1, 1, 0), ("subtitle", "eng", "", 0, 1, 0, 0),
                  ("subtitle", "por", "", 0, 1, 1, 0), ("subtitle", "eng", "English (Forced)", 0, 1, 0, 0)), "English")
    assert d["edits"] == [["track:s2", 0, 1], ["track:s3", 0, 1]] and "forced por" in d["notes"][0], d
    # English already plays first: the flags change only to leave exactly one default audio track, on the track that plays
    assert decide.decide(ff(("audio", "eng", "", 6, 1, 0, 0), ("audio", "rum", "", 6, 0, 0, 0)), "English")["edits"] == []
    d = decide.decide(ff(("audio", "eng", "", 6, 1, 0, 0), ("audio", "rum", "", 6, 1, 0, 0)), "English")
    assert d["edits"] == [["track:a2", 0, 1]] and d["rules"] == ["audio default flag fixed"], d
    assert decide.decide(ff(("audio", "eng", "", 6, 0, 0, 0), ("audio", "hin", "", 6, 0, 0, 0)), "English")["edits"] == [["track:a1", 1, 0]]
    assert [t["role"] for t in decide.classify(ff(("audio", "eng", "cmt", 2, 0, 0, 0), ("audio", "eng", "Director's Comm", 2, 0, 0, 0)))] == ["commentary"] * 2
    # a default in the right language stays, even a late track, and AC3 5.1 keeps its place before TrueHD 7.1
    assert decide.decide(ff(("audio", "eng", "French", 6, 0, 0, 0), ("audio", "por", "", 6, 0, 0, 0), ("audio", "eng", "English", 6, 1, 0, 0)), "English")["edits"] == []
    assert decide.decide(ff(("audio", "eng", "AC3 5.1", 6, 1, 0, 0), ("audio", "eng", "TrueHD 7.1", 8, 0, 0, 0)), "English")["edits"] == []
    # anime in mkvmerge shape with UIDs: jpn becomes the default audio, the full English subtitle beats signs and songs
    d = decide.decide({"tracks": [mk("video", "und", "", True), mk("audio", "eng", "English dub", True, uid=11, audio_channels=2),
                           mk("audio", "jpn", "", False, uid=12, audio_channels=2), mk("subtitles", "eng", "Signs & Songs", True, True, uid=13),
                           mk("subtitles", "eng", "Full", False, uid=14)]}, "Japanese")
    assert d["edits"] == [["track:=11", 0, 1], ["track:=12", 1, 0], ["track:=13", 0, 1], ["track:=14", 1, 0]], d
    assert d["path"] == "foreign original, original audio", d["path"]
    # foreign audio: a title-only "Forced" English subtitle and a commentary subtitle never beat the full one
    d = decide.decide(ff(("audio", "fre", "", 6, 1, 0, 0), ("subtitle", "eng", "English Forced", 0, 0, 0, 0), ("subtitle", "eng", "Commentary", 0, 0, 0, 0),
                  ("subtitle", "eng", "English", 0, 0, 0, 0)), "French")
    assert d["edits"] == [["track:s3", 1, 0]], d
    # foreign audio with no English subtitle: only a forced foreign subtitle loses its default flag
    d = decide.decide(ff(("audio", "jpn", "", 2, 1, 0, 0), ("subtitle", "por", "", 0, 1, 1, 0), ("subtitle", "spa", "", 0, 1, 0, 0)), "Japanese")
    assert d["edits"] == [["track:s1", 0, 1]], d
    # foreign title with an English dub titled English and a third language: the English rule. A bare eng tag abstains.
    d = decide.decide(ff(("audio", "spa", "", 2, 1, 0, 0), ("audio", "eng", "English", 2, 0, 0, 0), ("subtitle", "eng", "", 0, 1, 0, 0)), "Japanese")
    assert d["edits"] == [["track:a1", 0, 1], ["track:a2", 1, 0], ["track:s1", 0, 1]] and not d["wrong_language"], d
    assert d["edit_rules"] == ["audio switched", "audio switched", "English subtitle off"], d["edit_rules"]
    d = decide.decide(ff(("audio", "spa", "", 2, 1, 0, 0), ("audio", "eng", "", 2, 0, 0, 0), ("subtitle", "eng", "", 0, 1, 0, 0)), "Japanese")
    assert d["abstain"] == "original_missing_bare_tag", d
    # an untagged main track may be the original: undecided. An unknown original with an untagged track is undecided too.
    d = decide.decide(ff(("audio", "und", "", 2, 1, 0, 0), ("audio", "eng", "", 6, 0, 0, 0)), "Japanese")
    assert d["edits"] == [] and "untagged" in d["undecided"] and d["reasons"] == ["untagged_may_be_original"], d
    assert decide.decide(ff(("audio", "eng", "", 2, 1, 0, 0), ("audio", "und", "", 6, 0, 0, 0)), None)["undecided"]
    assert decide.codes(None) == decide.codes("") == decide.codes("und") == decide.codes("Unknown") == set()
    # Japanese series with only Portuguese and Spanish audio: nothing changes, wrong language
    d = decide.decide(ff(("audio", "por", "", 2, 1, 0, 0), ("audio", "spa", "", 2, 0, 0, 0), ("subtitle", "por", "", 0, 1, 0, 0)), "Japanese")
    assert d["edits"] == [] and d["wrong_language"] and not d["undecided"], d
    # untagged audio is never the wrong language
    d = decide.decide(ff(("audio", "und", "", 6, 1, 0, 0), ("audio", "por", "", 2, 0, 0, 0)), "English")
    assert d["edits"] == [] and not d["wrong_language"], d
    # commentary and audio description never become the default, the 6ch English track wins over the 2ch one
    d = decide.decide(ff(("audio", "eng", "Director's Commentary", 2, 1, 0, 0), ("audio", "eng", "", 2, 0, 0, 0), ("audio", "eng", "", 6, 0, 0, 0),
                  ("audio", "por", "", 6, 0, 0, 0)), "English")
    assert d["edits"] == [["track:a1", 0, 1], ["track:a3", 1, 0]] and "a3 (6ch) plays first" in d["notes"][0], d
    assert decide.decide(ff(("audio", "eng", "English AD", 2, 1, 0, 0), ("audio", "eng", "English", 2, 0, 0, 0)), "English")["edits"] == [
        ["track:a1", 0, 1], ["track:a2", 1, 0]]
    assert decide.classify(ff(("audio", "eng", "DVS", 2, 1, 0, 0)))[0]["role"] == "description"
    assert decide.classify(ff(("audio", "eng", "Adventure", 2, 1, 0, 0)))[0]["role"] == "main"
    # English only as commentary (flag or title): the wrong language, nothing changes
    d = decide.decide(ff(("audio", "por", "", 6, 1, 0, 0), ("audio", "eng", "", 2, 0, 0, 1)), "English")
    assert d["edits"] == [] and d["wrong_language"], d
    # checks. A header of 10:05 on a file whose bitrate says 62 minutes.
    size = 4.6e9
    hdr = {"container": {"properties": {"duration": 605 * 10**9, "writing_application": "mkvmerge v92.0"}},
            "tracks": [{"type": "video", "properties": {"pixel_dimensions": "1920x1080", "tag_bps": "9400000"}},
                       {"type": "audio", "properties": {"tag_bps": "448000"}}]}
    assert [k for k, _ in decide.checks(hdr, size, 62)] == ["duration"] and "about 62 minutes" in decide.checks(hdr, size, 62)[0][1]["why"]
    # stale BPS tags from ffmpeg are ignored, and so is a partial set. The 50 Mbit/s ceiling decides.
    remux_tag = {"container": {"properties": {"duration": 7200 * 10**9, "writing_application": "Lavf60.16.100"}},
                 "tracks": [{"type": "video", "properties": {"pixel_dimensions": "1920x1080", "tag_bps": "30000000"}}]}
    assert decide.checks(remux_tag, 8e9, 120) == []
    remux_tag["container"]["properties"]["writing_application"] = "mkvmerge v92.0"
    remux_tag["tracks"].append({"type": "audio", "properties": {}})
    assert decide.checks(remux_tag, 8e9, 120) == []
    del hdr["tracks"][0]["properties"]["tag_bps"]
    assert [k for k, _ in decide.checks(hdr, size, 62)] == ["duration"]
    assert decide.checks({"container": {"properties": {"duration": 7200 * 10**9}}, "tracks": []}, 7200 * 45e6 / 8, 120) == []
    # runtime. Sonarr alerts only on a short file.
    short = {"container": {"properties": {"duration": 605 * 10**9}}, "tracks": [{"type": "video", "properties": {"pixel_dimensions": "1920x1080"}}]}
    assert decide.checks(short, 0.5e9, 62) == [("runtime", {"runs": "10:05", "listed": 62})]
    assert decide.checks(short, 0.5e9, 9) == [] and decide.checks(short, 0.5e9, 11) == []
    double = {"container": {"properties": {"duration": 5400 * 10**9}}, "tracks": []}
    assert [k for k, _ in decide.checks(double, 2e9, 44)] == ["runtime"] and decide.checks(double, 2e9, 44, shorter_only=True) == []
    assert [k for k, _ in decide.checks(short, 0.5e9, 62, shorter_only=True)] == ["runtime"]
    assert decide.codes("Portuguese (Brazil)") == {"por", "pob"} and decide.codes("German") == {"ger", "deu"} and decide.codes("jpn") == {"jpn", "jap"}
    # mkvmerge records with statistics: 100 minutes, so 150 events are 1.5 a minute
    def rec(*tracks):
        return {"container": {"properties": {"duration": 6000 * 10**9, "writing_application": "mkvmerge v92.0"}},
                "tracks": [mk(typ, lang, name, dflt, forced, uid=uid, **kw) for typ, lang, name, dflt, forced, uid, kw in tracks]}
    # "English - Alien Only" stays on under English audio, and so does a sparse unflagged track
    d = decide.decide(rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "eng", "English - Alien Only", True, False, 2, {})), "English")
    assert d["edits"] == [], d
    d = decide.decide(rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "eng", "", True, False, 2, {"tag_number_of_frames": "40"})), "English")
    assert d["edits"] == [], d
    # a full English track goes off, so the forced English track comes on
    d = decide.decide(rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "eng", "", True, False, 2, {"tag_number_of_frames": "1500"}),
                   ("subtitles", "eng", "Forced", False, True, 3, {"tag_number_of_frames": "30"})), "English")
    assert d["edits"] == [["track:=2", 0, 1], ["track:=3", 1, 0]], d
    assert d["rules"] == ["English subtitle off", "forced English subtitle on"], d["rules"]
    # a dense track flagged forced is a full subtitle. With English the only audio language and TMDB listing English
    # alone, it goes off and loses the forced flag. With other spoken languages or TMDB unknown, the flag stays and the
    # decision abstains. Under 10 events a minute the flag is the only sign of what the release meant to show, so the
    # decision abstains too.
    dense_cc = rec(("audio", "eng", "English", True, False, 1, {}), ("subtitles", "eng", "CC", True, True, 2, {"tag_number_of_frames": "1500"}))
    d = decide.decide(dense_cc, "English", spoken=["eng"])
    assert d["edits"] == [["track:=2", 0, 1], ["track:=2", 0, 1, decide.FORCED_FLAG]] and "forced_flag_cleared_dense" in d["reasons"], d
    assert d["edit_rules"] == ["English subtitle off", "forced flag cleared"] and decide.invariants(d["tracks"], d["edits"], "english", {"eng"}) == [], d
    for spoken, code in ((None, "forced_flag_kept_tmdb_unknown"), (["eng", "por", "spa"], "forced_flag_kept_spoken")):
        d = decide.decide(dense_cc, "English", spoken=spoken)
        assert d["edits"] == [] and d["abstain"] == "dense_forced_flag_english_only" and code in d["reasons"], d
    # a foreign original that plays its English dub needs no TMDB answer
    assert decide.decide(dense_cc, "French")["edits"] == [["track:=2", 0, 1], ["track:=2", 0, 1, decide.FORCED_FLAG]]
    d = decide.decide(rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "eng", "", True, True, 2, {"tag_number_of_frames": "650"})), "English")
    assert d["edits"] == [] and "flagged forced but has 6.5 events a minute, English is the only" in d["undecided"], d
    # the same track in a Hindi dual-audio release goes off: the flag is for the Hindi audio
    d = decide.decide(rec(("audio", "hin", "", True, False, 1, {}), ("audio", "eng", "", False, False, 2, {}),
                   ("subtitles", "eng", "English_Full", True, True, 3, {"tag_number_of_frames": "1500"})), "English")
    assert d["edits"] == [["track:=1", 0, 1], ["track:=2", 1, 0], ["track:=3", 0, 1]] and not d["undecided"], d
    # a PGS track counts two frames per subtitle: 500 frames in 100 minutes is 2.5 events a minute, flagged forced, so it stays on
    d = decide.decide(rec(("audio", "eng", "", True, False, 1, {}),
                   ("subtitles", "eng", "", True, True, 2, {"tag_number_of_frames": "500", "codec_id": "S_HDMV/PGS"})), "English")
    assert d["edits"] == [], d
    # a forced flag on a dense track does not count, and the dub transcript comes last
    d = decide.decide(rec(("audio", "fre", "", True, False, 1, {}), ("subtitles", "eng", "English (dub)", False, False, 2, {"tag_number_of_frames": "1400"}),
                   ("subtitles", "eng", "English", False, True, 3, {"tag_number_of_frames": "1500"})), "French")
    assert d["edits"] == [["track:=3", 1, 0]], d
    # the title beats the tag: audio tagged eng and titled French is French. "UK Dub / Japanese Score" stays English.
    d = decide.decide(rec(("audio", "eng", "French", True, False, 1, {}), ("subtitles", "eng", "", False, False, 2, {"tag_number_of_frames": "1500"})), "French")
    assert d["edits"] == [["track:=2", 1, 0]], d
    assert d["tracks"][0]["conf"] == decide.TITLE_WINS and decide.classify(rec(("audio", "eng", "English", True, False, 1, {})))[0]["conf"] == decide.AGREE
    assert decide.title_language("UK Dub / Japanese Score") is None and decide.title_language("国语") == "chi"
    assert decide.title_language("English Subtitles", "s") == "eng" and decide.title_language("English Subtitles") is None
    # an und tag takes the language its title names ("English DTS 5.1"), and so does a subtitle. An und "English forced"
    # track that translates sign language stays on under English audio.
    assert decide.classify(rec(("audio", "und", "English DTS 5.1", True, False, 1, {})))[0]["lang"] == "eng"
    signs = rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "und", "English forced", True, True, 2, {"tag_number_of_frames": "100"}))
    assert decide.decide(signs, "English")["edits"] == [], decide.decide(signs, "English")
    # a track titled SDH that measures sparse contradicts itself. Under other audio, when the best English track is then a
    # forced one, the decision abstains.
    sparse_sdh = rec(("audio", "por", "", True, False, 1, {}), ("subtitles", "eng", "English (SRT)", False, True, 2, {"tag_number_of_frames": "30"}),
                ("subtitles", "eng", "English (SDH)", True, False, 3, {"tag_number_of_frames": "130"}))
    assert decide.decide(sparse_sdh, "Portuguese")["abstain"] == "sparse_full_title", decide.decide(sparse_sdh, "Portuguese")
    # the app says Spanish, and the only audio is tagged eng with no language in its title: undecided. A title that names
    # English, or a release name that does, lets the file decide.
    bare = rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "eng", "English", True, True, 2, {"tag_number_of_frames": "655"}))
    assert decide.decide(bare, "Spanish")["undecided"].startswith("the app says Spanish") and decide.decide(bare, "Spanish")["reasons"] == ["original_missing_bare_tag"]
    dub = rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "eng", "", True, False, 2, {"tag_number_of_frames": "1500"}))
    assert decide.decide(dub, "Spanish")["undecided"] and decide.decide(dub, "Spanish", release="Film.A.2009.ENGLiSH.1080p")["edits"] == [["track:=2", 0, 1]]
    titled = rec(("audio", "eng", "English", True, True, 1, {}), ("subtitles", "eng", "English", True, False, 2, {}))
    assert decide.decide(titled, "Japanese", kids=True)["edits"] == [["track:=2", 0, 1]]
    # kids rule: a Japanese kids film with an English dub plays the dub with subtitles off. Without the flag it plays Japanese.
    kid = rec(("audio", "jpn", "", True, False, 1, {}), ("audio", "eng", "", False, False, 2, {}),
              ("subtitles", "eng", "", True, False, 3, {"tag_number_of_frames": "1500"}))
    d = decide.decide(kid, "Japanese", kids=True)
    assert d["edits"] == [["track:=1", 0, 1], ["track:=2", 1, 0], ["track:=3", 0, 1]] and d["path"] == "foreign kids title, English dub", d
    assert decide.decide(kid, "Japanese")["edits"] == []
    assert decide.kids_title("radarr", ["Animation", "Action"]) is False and decide.kids_title("sonarr", ["Children"]) is True
    assert decide.kids_title("radarr", ["Drama"], "Kids") is True
    assert decide.kids_title("radarr", ["Fantasy", "Animation"], studio="Studio A") is True
    # invariants on hand-made bad states: two audio defaults after an audio move, commentary default, a full English subtitle on
    # under English audio, and an English original that plays Portuguese
    ts = decide.classify(rec(("audio", "por", "", True, False, 1, {}), ("audio", "eng", "", False, False, 2, {}),
                      ("audio", "eng", "Commentary", False, False, 3, {}), ("subtitles", "eng", "", False, False, 4, {"tag_number_of_frames": "1500"})))
    text = lambda broken: [t for _, t in broken]
    assert text(decide.invariants(ts, [["track:=1", 0, 1], ["track:=2", 1, 0], ["track:=3", 1, 0], ["track:=4", 1, 0]], "english", {"eng"})) == [
        "2 audio tracks would be default", "commentary track a3 would be default", "full English subtitle s1 would stay on under English audio"]
    assert decide.invariants(ts, [["track:=4", 1, 0]], "english", {"eng"}) == [
        ("inv_audio_not_policy_target", "the English original would play por audio, the policy wants original")]
    # the invariants follow the policy: with SDH allowed under English audio, an SDH track may stay on
    ts2 = decide.classify(rec(("audio", "eng", "", True, False, 1, {}), ("subtitles", "eng", "SDH", True, False, 2, {"tag_number_of_frames": "1500"})))
    assert [c for c, _ in decide.invariants(ts2, [], "english", {"eng"})] == ["inv_full_english_subtitle_on"]
    decide.set_policy(dict(TEST_POLICY, subtitles=dict(TEST_POLICY["subtitles"], english=["forced", "sdh"])))
    try:
        assert decide.invariants(ts2, [], "english", {"eng"}) == []
    finally:
        decide.set_policy(TEST_POLICY)
    assert decide.invariants(ts, [["track:=1", 0, 1], ["track:=2", 1, 0]], "english", {"eng"}) == []
    # audio verdicts. ffmpeg's output line says 48 kHz stereo, so 20 seconds decode 1,920,000 samples.
    ok = {"n": 1922128, "max": -4.0, "errors": 0, "window": 20, "ran": True, "cut": False, "rate": 48000, "ch": 2}
    quiet = dict(ok, max=-91.0); seek = dict(ok, errors=1); lossy = dict(ok, n=1500000, errors=3)
    empty = dict(ok, n=0, max=None); failed = dict(empty, ran=False, errors=2)
    none = {}
    assert decide.audio_verdict(none, 0, [ok, ok, ok]) == (None, [])
    assert decide.audio_verdict(none, 0, [seek, seek, seek]) == (None, [])   # an error at the seek, nothing lost
    assert decide.audio_verdict(none, 0, [quiet, quiet, quiet])[0] == "the audio is silent at all 3 places checked"
    assert decide.audio_verdict(none, 0, [quiet, ok, ok]) == (None, ["the audio is silent at 1 of 3 places checked"])
    assert decide.audio_verdict(none, 0, [lossy, lossy, lossy])[0] == "the audio fails to play at all 3 places checked"
    cut = dict(empty, cut=True, errors=1, at=6120)   # the late sample of a 120-minute film starts at 102 minutes
    assert decide.audio_verdict(none, 0, [ok, ok, cut], runtime=120)[0] == "the file is cut off before the last place checked"
    assert decide.audio_verdict(none, 0, [ok, ok, cut])[0] is None                  # no listed runtime, no cross-check
    assert decide.audio_verdict(none, 0, [ok, ok, cut], runtime=100)[0] is None     # the late sample starts past 95 percent of it
    tail = ["the file may end early, near the last place checked"]
    assert decide.audio_verdict(none, 0, [ok, ok, dict(ok, n=1899000, cut=True, errors=1)]) == (None, tail)
    assert decide.audio_verdict(none, 0, [ok, ok, dict(ok, n=1286000, cut=True, errors=1)])[0] is None   # 67 percent, still only a doubt
    assert decide.audio_verdict(none, 0, [ok, ok, empty]) == (None, ["no audio plays at the last place checked, maybe because the file says it runs longer than it does"])
    assert decide.audio_verdict(none, 0, [failed, failed, failed]) == (None, ["the audio check could not run at 3 of 3 places"])   # EACCES, bad map
    assert decide.audio_verdict(none, 0, [quiet, quiet, failed]) == (None, ["the audio check could not run at 1 of 3 places"])
    assert decide.audio_verdict(none, None, []) == ("the file has no audio track", [])
    assert decide.audio_verdict(none, 0, [ok, dict(ok, n=960000), ok]) == (None, ["part of the audio is missing, and only 50% of it plays where it was checked"])
    # a track the container calls 6 channels decodes 2: the ratio uses ffmpeg's 2, so seek errors lose nothing
    assert decide.audio_verdict({"tracks": [{"type": "audio", "properties": {"audio_channels": 6}}]}, 0, [seek, seek, seek]) == (None, [])
    tagged = {"container": {"properties": {"writing_application": "mkvmerge v92.0"}},
              "tracks": [{"type": "video", "properties": {"tag_duration": "01:30:00.000000000"}},
                         {"type": "audio", "properties": {"tag_duration": "01:00:00.000000000"}}]}
    assert decide.audio_verdict(tagged, 0, [ok, ok, ok]) == (None, ["the audio stops at 1:00:00, but the video runs to 1:30:00"])
    got = decide.parse_sample("[x] [info] n_samples: 0\n[aac] [error] bad\n[m] [error] File is broken, keyframes not correctly marked!\n"
                       "[info] Output #0, null, to 'pipe:':\n[info]   Stream #0:0: Audio: pcm_s16le, 48000 Hz, 5.1(side), s16, 4608 kb/s\n"
                       "[x] [info] n_samples: 20\n[x] [info] max_volume: -91.0 dB")
    assert got == {"n": 20, "max": -91.0, "errors": 1, "ran": True, "cut": False, "rate": 48000, "ch": 6}, got
    assert decide.parse_sample("[fatal] Stream map '' matches no streams.", 234)["ran"] is False
    assert [decide.channels(x) for x in ("mono", "stereo", "5.1(side)", "7.1", "2.1", "quad", "12 channels", "weird")] == [1, 2, 6, 8, 3, 4, 12, None]
    assert decide.codes("Latvian") == {"lav"} and decide.codes("Irish") == {"gle"} and decide.codes("Maltese") == {"mlt"}
    # a heard language that agrees makes a bare tag certain. One that disagrees wins at HEARD (a Spanish film tagged eng).
    assert decide.language("eng", None, "eng") == ("eng", decide.AGREE, "tag only, heard eng") and decide.language("jap", None, "jpn")[1] == decide.AGREE
    assert decide.language("eng", None, "spa")[:2] == ("spa", decide.HEARD) and decide.language("und", None, "eng")[:2] == ("eng", decide.HEARD)
    d = decide.decide(bare, "Spanish", heard={"a1": "spa"})
    assert d["edits"] == [] and not d["undecided"] and d["path"] == "foreign original, original audio" and "heard_language" in d["reasons"], d
    d = decide.decide(dub, "Spanish", heard={"a1": "eng"})   # heard English confirms the bare tag, so the English dub plays with subtitles off
    assert d["edits"] == [["track:=2", 0, 1]] and "heard_confirms" in d["reasons"] and d["tracks"][0]["conf"] == decide.AGREE, d
    assert decide.decide(bare, "Spanish", heard={"a2": "spa"})["abstain"] == "original_missing_bare_tag"   # a subtitle position is never heard
    assert decide.decide(ff(("audio", "und", "", 2, 1, 0, 0), ("audio", "eng", "", 6, 0, 0, 0)), "Japanese", heard={"a1": "jpn"})["edits"] == []
    # language tags: a tag changes on two agreeing signals only, and the heard language is one of them
    table = decide.language_table("English language name | ISO 639-3 code | ISO 639-2 code | ISO 639-1 code\n-----+-----\n"
                           "English | eng | eng | en\nSpanish | spa | spa | es\nFrench | fre | fre | fr\nItalian | ita | ita | it\n"
                           "Portuguese | por | por | pt\nHindi | hin | hin | hi\nKorean | kor | kor | ko\nChinese | chi | chi | zh\n"
                           "Japanese | jpn | jpn | ja\nYue Chinese | yue | |\nFilipino | fil | fil |\nUndetermined | und | und |")
    assert table[0]["en"] == "eng" and table[1]["fil"] == "fil" and "yue" not in table[0] and decide.ietf_form("ZH-hant-tw-x-a") == "zh-Hant-TW"
    tagged = lambda *ts: rec(*[(typ, lang, name, dflt, False, uid, {"language_ietf": ietf} if ietf else {}) for typ, lang, ietf, name, dflt, uid in ts])
    langs = tagged(("audio", "und", None, "English", True, 1), ("audio", "und", None, "Español", False, 2), ("audio", "und", None, "Português", False, 3),
                   ("audio", "und", None, "Commentary", False, 4))   # und tracks titled by language
    assert decide.retag(langs, table=table)["ask"] == {"a1", "a2", "a3"}   # und main tracks are heard first, the commentary never
    r = decide.retag(langs, {"a1": "eng", "a2": "spa", "a3": "por"}, {"eng"}, table)
    assert r["edits"] == [["track:=1", "en", "und", decide.LANG_EDIT], ["track:=1", "en", None, decide.LANG_IETF],   # the undo deletes the new BCP 47 tag
                          ["track:=2", "es", "und", decide.LANG_EDIT], ["track:=2", "es", None, decide.LANG_IETF]] and r["set"] == {"a1": "eng", "a2": "spa"}, r
    assert r["rules"] == ["language tag set"] * 4 and r["notes"][2].startswith("a3 keeps und: por (heard por)"), r
    spa_eng = tagged(("audio", "eng", "en", "", True, 1), ("subtitles", "eng", "en", "", False, 2))   # a Spanish film tagged eng
    assert decide.retag(spa_eng, {"a1": "spa"}, {"spa"}, table)["edits"] == [["track:=1", "es", "eng", decide.LANG_EDIT]]
    spoken_eng = decide.retag(spa_eng, {"a1": "spa"}, {"spa"}, table, spoken={"eng", "spa"})   # TMDB lists English as spoken, which never backs the tag
    assert spoken_eng["edits"] == [["track:=1", "es", "eng", decide.LANG_EDIT]] and spoken_eng["notes"] == ["a1 eng/en -> es: heard spa, and the item's original language"]
    assert decide.retag(spa_eng, {"a1": "fre"}, {"eng"}, table, spoken={"eng", "fre"})["edits"] == []   # French heard in an English film stays a note
    assert decide.retag(spa_eng, {"a1": "kor"}, {"eng"}, table)["edits"] == [] and decide.retag(spa_eng, {}, {"spa"}, table)["edits"] == []
    ja_title = tagged(("audio", "eng", "en", "Japanese", False, 1), ("audio", "eng", None, "English", True, 2))   # a Japanese film, both tracks tagged eng
    r = decide.retag(ja_title, {}, {"jpn"}, table)   # the title and the original agree, but nothing heard it: the tag stays until the heard language agrees
    assert r["edits"] == [] and r["ask"] == {"a1"} and r["notes"] == ['a1 keeps eng/en: eng (tagged eng); jpn (the title "Japanese"), no heard language'], r
    assert decide.retag(ja_title, {"a1": "jpn"}, {"jpn"}, table)["edits"] == [["track:=1", "ja", "eng", decide.LANG_EDIT]]
    assert decide.retag(ja_title, {"a1": "eng"}, {"jpn"}, table)["edits"] == []   # the muxer copied the title onto the English dub
    dub = tagged(("audio", "eng", "en-US", "Hindi", True, 1))   # a Hindi dub tagged English: heard and the title beat the tag and the item
    assert decide.retag(dub, {"a1": "hin"}, {"eng"}, table)["edits"] == [["track:=1", "hi", "eng", decide.LANG_EDIT], ["track:=1", "hi", "en-US", decide.LANG_IETF]]
    fre_und = tagged(("audio", "fre", "und", "", True, 1))   # the BCP 47 tag says und, TMDB says French
    assert decide.retag(fre_und, {}, {"fre"}, table)["edits"] == [] and decide.retag(fre_und, table=table)["ask"] == {"a1"}
    assert decide.retag(fre_und, {"a1": "fre"}, {"fre"}, table)["edits"] == [["track:=1", "fr", "fre", decide.LANG_EDIT], ["track:=1", "fr", "und", decide.LANG_IETF]]
    yue = tagged(("audio", "und", "yue", "", True, 1), ("audio", "und", "cmn-Hant", "", False, 2))   # a BCP 47 tag inside Chinese stays
    assert [e[1] for e in decide.retag(yue, {"a1": "chi", "a2": "chi"}, {"chi"}, table)["edits"]] == ["yue", "yue", "cmn-Hant", "cmn-Hant"]
    by_hand = tagged(("audio", "eng", "en", "", True, 1), ("subtitles", "eng", "en", "", True, 2))   # the PGS track tagged by hand
    assert decide.retag(by_hand, {}, {"eng"}, table)["edits"] == [] and decide.retag(by_hand, {}, {"eng"}, table)["notes"] == [] and not decide.retag(by_hand, table=table)["ask"]
    subs = tagged(("subtitles", "chi", "zh-Hant", "Traditional", False, 1), ("subtitles", "eng", "en-us", "", False, 2),
                  ("subtitles", "und", None, "English", False, 3), ("subtitles", "eng", "fr-CA", "French", False, 4), ("audio", "zxx", None, "", True, 5))
    r = decide.retag(subs, {}, {"eng"}, table)   # a subtitle is never heard, so it keeps its language, even when its two tags disagree
    assert r["edits"] == [["track:=2", "en-US", "eng", decide.LANG_EDIT], ["track:=2", "en-US", "en-us", decide.LANG_IETF]] and r["set"] == {} and not r["ask"], r
    assert r["rules"] == ["language tag form"] * 2 and r["notes"][-1].startswith("s4 keeps eng/fr-CA"), r
    # subtitle text: the read language is one more signal, and a read language with no second signal keeps the tag
    en = ["Where have you been? I called you three times."] * 10
    assert decide.text_language(en)[0] == "eng" and decide.text_language(en[:2])[0] is None
    st = tagged(("audio", "eng", None, "", True, 1), ("subtitles", "eng", None, "", True, 2), ("subtitles", "eng", None, "French", False, 3))
    r = decide.retag(st, table=table, read={"s1": "rum", "s2": "fre"})
    assert r["mismatch"] == ["subtitle track 1 is tagged English, but its text reads as Romanian"] and r["set"] == {"s2": "fre"} and decide.retag(st, table=table)["to_read"] == {"s2"}, r
    assert decide.sidecar_language("eng", ("rum", 1.0, ""), {"eng"})[:2] == ("rum", False) and decide.sidecar_language("eng", ("eng", 1.0, ""), {"eng"}) is None
    assert decide.retag(st, table=table, read={"s1": "rum"})["wrong"] == {"s1": "rum"}   # a lone wrong language loses the default it has
    signs = tagged(("audio", "eng", None, "", True, 1), ("subtitles", "eng", None, "Forced", True, 2))   # a forced English track stays on
    assert decide.decide(signs, "English", wrong={"s1": "rum"})["edits"] == [["track:=2", 0, 1]] and decide.decide(signs, "English")["edits"] == []
    und_sub = tagged(("audio", "eng", None, "", True, 1), ("subtitles", "und", None, "", False, 2))
    assert decide.retag(und_sub, table=table, read={"s1": "fre"})["set"] == {"s1": "fre"}   # an und tag claims nothing, so the read language sets it
    und = tagged(("audio", "und", None, "English", True, 1))
    assert decide.retag(und, {"a1": "spa"}, {"eng"}, table, spoken={"spa"})["edits"] == []   # a tie: the title and the original against heard and spoken
    assert decide.retag(tagged(("audio", "und", None, "", True, 1)), {"a1": "cze"}, {"eng"}, table, spoken={"cze"})["edits"][0][1] == "cze"   # spoken, und only
    d = decide.with_tags(decide.decide(langs, "English"), r := decide.retag(langs, {"a1": "eng"}, {"eng"}, table))
    assert d["edits"][-2:] == [["track:=1", "en", "und", decide.LANG_EDIT], ["track:=1", "en", None, decide.LANG_IETF]] and decide.plan_class(d) == "English original: language tag set", d
    assert decide.with_tags(dict(d, undecided="x", edits=[]), r)["edits"] == []
    after = tagged(("audio", "eng", "en", "English", True, 1))
    assert decide.unapplied(after, [["track:=1", "en", "und", decide.LANG_EDIT], ["track:=1", 1, 0]]) == [] and decide.unapplied(after, [["track:=1", "es", "und", decide.LANG_EDIT]])
    # the video verdict: seek noise before the first frame, damage after it, the null muxer, lost frames and a cut
    frames = lambda n, t0=0.0: "\n".join(f"[Parsed_showinfo_0 @ 0x1] [info] n: {i} pts: {i} pts_time:{t0 + i / 24:.6g} duration:1"
                                         for i in range(n))
    seek = "[h264 @ 0x2] [error] co located POCs unavailable\n[hevc @ 0x3] [error] PPS changed between slices.\n"
    w = decide.parse_window(seek + frames(120))
    assert (w["frames"], w["errors"], w["late"], w["empty"]) == (120, 0, False, False) and decide.bad_window(w) is None, w
    dmg = decide.parse_window(frames(120) + "\n[h264 @ 0x2] [error] error while decoding MB 4 12, bytestream 33215")
    assert dmg["errors"] == 1 and decide.bad_window(dmg) == "1 playback error"
    assert decide.bad_window(decide.parse_window(frames(120) + "\n[matroska,webm @ 0x3] [error] 0x00 at pos 12 (0xc) invalid as first byte of an EBML number"))
    assert decide.parse_window(frames(120) + "\n[vist#0:0/h264 @ 0x4] [dec:h264 @ 0x5] [warning] corrupt decoded frame")["errors"] == 1
    assert decide.parse_window(frames(120) + "\n[null @ 0x4] [error] Application provided invalid, non monotonically increasing dts to muxer")["errors"] == 0
    gap = decide.parse_window(frames(30) + "\n" + frames(30, 3.0))
    assert gap["gap"] > decide.GAP and decide.bad_window(gap).endswith("missing")
    late = decide.parse_window(frames(50, 612.4))   # the seek landed in zeros, and video came back 612 s on
    assert decide.bad_window(late) == "no video" and decide.bad_window(decide.parse_window(frames(120, 7.3))) is None   # a TS seek
    empty = decide.parse_window("[out#0/null @ 0x6] [warning] Output file is empty, nothing was encoded")
    assert empty["empty"] and not empty["noisy"] and decide.bad_window(empty) is None
    cut = decide.parse_window("[matroska,webm @ 0x3] [error] File ended prematurely")
    assert cut["cut"] and cut["ran"] and not cut["noisy"] and cut["errors"] == 0 and decide.bad_window(cut) is None
    scan = decide.parse_window("[matroska,webm @ 0x3] [error] Element at 0x80000000 ending at 0x500000000 exceeds containing master element\n"
                        + frames(120))   # a seek with no index read through the damage
    assert scan["errors"] == 1 and decide.bad_window(scan)
    at = lambda w, s, **k: dict(w, at=s, **k)
    ok = at(w, 300)
    assert decide.video_verdict([], [ok, ok, ok]) == (None, [])
    assert decide.video_verdict([], [at(dmg, 150), ok, ok]) == (None, ["1 playback error at 2:30"])
    assert decide.video_verdict([], [at(dmg, 150), ok, at(late, 2550)])[0] == (
        "the video is broken at 2 of 3 places checked: 1 playback error at 2:30 and no video at 42:30")
    assert decide.video_verdict([], [ok, ok, at(empty, 2550)]) == (None, ["no video at 42:30"])
    assert decide.video_verdict([0.3, 0.5], [])[0].startswith("the file has blank gaps at 2 of 256")
    assert decide.video_verdict([0.3], [ok, ok, ok]) == (None, ["the file has a blank gap at 30% of its length"])
    assert decide.video_verdict([], [ok, ok, at(cut, 2550)]) == (None, ["no video at 42:30, where the file is cut off"])
    capped = at(decide.parse_window(frames(3), rc=1), 1502, stopped="read over 512 MiB")   # a killed window never counts as bad
    assert decide.video_verdict([], [capped, capped, ok]) == (None, [])   # the read cap alone is no doubt
    slow = dict(capped, stopped="ran over 60 s")
    assert decide.video_verdict([], [slow, ok, ok]) == (None, ["the video check at 25:02 ran over 60 s, so the file may have no usable index"])
    assert decide.bad_window(dict(empty, noisy=True, cut=True)) == "no video"
    assert decide.parse_window("[mpeg4 @ 0x7] [error] low_delay flag set incorrectly, clearing it\n" + frames(120))["errors"] == 0
    unknown = decide.parse_window("[vist#0:0/none @ 0x8] [error] Decoding requested, but no decoder found for: none\n[fatal] Error opening output files", 234)
    assert unknown["nodecoder"] and not unknown["encrypted"] and decide.video_verdict([], [at(unknown, 263), at(unknown, 1316), ok])[0] is None
    enc = decide.parse_window("[matroska,webm @ 0x9] [error] mov FourCC not found encv.\n" + "[vist#0:0/none @ 0x8] [error] Decoding requested, but no "
                       "decoder found for: none\n[fatal] Error opening output files", 234)
    assert enc["encrypted"] and decide.video_verdict([], [at(enc, 263), at(enc, 1316), ok])[0].startswith("the video is encrypted")
    # the header check's parser: a Cluster at 1000 ms with a video block and a laced audio block of 3 frames, then Cues
    el = lambda i, data: i.to_bytes((i.bit_length() + 7) // 8, "big") + bytes([0x80 | len(data)]) + data
    block = lambda track, rel, lace=b"": bytes([0x80 | track]) + rel.to_bytes(2, "big") + (b"\x02" + lace if lace else b"\x00") + b"data"
    cluster = el(decide.CLUSTER, el(decide.TIMESTAMP, (1000).to_bytes(2, "big")) + el(decide.SIMPLEBLOCK, block(1, 0)) + el(decide.SIMPLEBLOCK, block(2, 40, b"\x02")))
    cues = el(decide.CUES, el(decide.CUEPOINT, el(decide.CUETIME, (1000).to_bytes(2, "big")) + el(decide.CUETRACKPOS, el(decide.CUETRACK, b"\x01"))))
    assert decide.stream_ends(b"\x1f\x43\xb6\x75junk" + cluster + cues, {1: 42, 2: 32}) == {1: 1042, 2: 1136}   # a Cluster id in data never counts
    assert decide.stream_ends(cluster[:-3], {1: 42, 2: 32}) == {1: 1042} and decide.stream_ends(b"no clusters here", {}) is None
    assert decide.last_cues(cues[decide.element(cues, 0)[1]:]) == {1: 1000}
    assert (decide.remove_track(24, 21), decide.remove_track(6, 3), decide.remove_track(6, 1), decide.remove_track(502, 1)) == (True, True, False, False)
    assert decide.subtitle_plan([], [15], {15: 10033}, 6737, 112) == ([], [15], None)   # a track timed for another cut
    assert decide.subtitle_plan([2], [], {2: 3471}, 1172, 20) == ([2], [], None)   # one runaway line
    assert decide.subtitle_plan([], [2], {2: 1391}, 1187, 24) == ([], [], "the video and audio stop at 19:47, but the listed runtime is 24 "
                                                                   "minutes, and the subtitles run to 23:11")   # a cut file
    assert decide.subtitle_plan([], [2], {2: 125}, 118, 2) == ([2], [], None)   # complete, many late lines ending near the end: a trim
    assert decide.subtitle_plan([2], [], {2: 900}, 700, 0)[2].endswith("no runtime is listed to tell whether the file is cut short")
    assert decide.subtitle_plan([], [2], {2: 3600}, 600, 12)[2].startswith("the video and audio stop at 10:00")   # a cut plus one runaway line
    srt = "1\n00:00:01,000 --> 00:00:02,500\nkept\n\n2\n00:01:50,000 --> 13:30:00,000\ncut\ntwo lines\n\n3\n00:02:10,000 --> 00:02:11,000\ngone\n"
    assert decide.trim_srt(srt, 120.023) == ("1\n00:00:01,000 --> 00:00:02,500\nkept\n\n2\n00:01:50,000 --> 00:02:00,023\ncut\ntwo lines\n",
                                      {"events": 3, "cut": 1, "dropped": 1})
    blank = "1\n00:00:01,000 --> 00:00:02,000\nfirst\n\nafter a blank\n\n2\n00:03:00,000 --> 00:03:01,000\ngone\n\nwith it\n"
    assert decide.trim_srt(blank, 120) == ("1\n00:00:01,000 --> 00:00:02,000\nfirst\n\nafter a blank\n", {"events": 2, "cut": 0, "dropped": 1})
    try:
        decide.trim_srt("no timing\n\n" + srt, 120)
        raise AssertionError("a first block with no timing line must raise")
    except ValueError:
        pass
