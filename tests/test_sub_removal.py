# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Which subtitle that does not match the audio leaves the file (docs/design.md, "Subtitle match"). Before a mismatch
counts, every other audio track in the subtitle's language hears the same windows, see subtitles.sub_others(). A
track leaves only when its text reads as its tag, see subtitles.tag_read(), and its text then goes beside the video,
see remux.resub(). The flow tests fake the model, see the env fixture. Every file and every line is made up."""
import dataclasses
import os
import re
import shutil
import sys
import types

import pytest

import amg
from test_arr_media_guard import (LANGUAGES, REAL_MKVMERGE, REAL_RUN, decided, english_film, env, hearing, hook, log_lines,  # noqa: F401
                                  removal_film, sidecar_film, srt_text, state_dir_in_tmp, sub_probe, talk)

DUTCH = ["Waar is het huis van mijn broer?", "Ik weet niet wat je daarmee bedoelt.", "We gaan morgenmiddag naar het strand.",
         "Ik moet met haar praten voordat ze weggaat.", "Waarom heb je me dat niet eerder verteld?", "De hond eet elke dag in de keuken."]
FINNISH = ["Missä on veljeni talo?", "En tiedä mitä tarkoitat sillä.", "Menemme huomenna rannalle iltapäivällä.",
           "Minun täytyy puhua hänen kanssaan ennen kuin hän lähtee.", "Miksi et kertonut minulle aikaisemmin?", "Koira syö keittiössä joka päivä."]
LOOKS = dict(zip("aeocpxy", "аеосрху"))   # Latin letters and the Cyrillic letters that look the same
RUSSIAN = ["Где дом моего брата?", "Я не знаю, что ты имеешь в виду.", "Мы завтра пойдём на пляж.", "Мне нужно поговорить с ней до того, как она уйдёт.",
           "Почему ты не сказал мне раньше?", "Собака каждый день ест на кухне."]
GREEK = ["Πού είναι το σπίτι του αδελφού μου;", "Δεν ξέρω τι εννοείς.", "Θα πάμε στην παραλία αύριο το απόγευμα.", "Πρέπει να της μιλήσω πριν φύγει.",
         "Γιατί δεν μου το είπες νωρίτερα;", "Ο σκύλος τρώει στην κουζίνα κάθε μέρα."]


def said_in(cues, lines):
    """cues with the text of lines in their place, one line a cue in turn."""
    return [(a, b, lines[i % len(lines)]) for i, (a, b, _) in enumerate(cues)]


def looks_alike(cues):
    """cues with every Latin letter that has a Cyrillic look-alike swapped for it. A player shows the same text, but
    the letters are others."""
    return [(a, b, "".join(LOOKS.get(c, c) for c in t)) for a, b, t in cues]


def ocr_swapped(cues):
    """cues in the swap pattern of a real OCR track: every e, c and x, every y but the one after r, the capitals A, B,
    C, E, H, M, O and P, and each T but the one before o, a, w or e, as their Cyrillic look-alikes. a, o and p stay Latin."""
    swap = lambda t: re.sub(r"(?<!r)y", "у", re.sub(r"T(?![oawe])", "Т", t)).translate(str.maketrans("ecxABCEHMOP", "есхАВСЕНМОР"))
    return [(a, b, swap(t)) for a, b, t in cues]


def heard_by(env, monkeypatch, cues, lines):
    """hearing() with the lines lines[i] spoken on ffmpeg audio stream i. A stream with no lines gives lines[i] as the
    answer of its hearing. env["words"] records (audio index, language, windows) per hearing, and env["more"] (audio
    index, the longer windows) per hearing."""
    hearing(env, monkeypatch, cues)

    def lid_run(path, index, j, expect, timeout, fresh=False, keep=False, words=None, then=None, yield_to=None):
        if not words:
            return {"why": "no language hearing in these tests"}
        env.setdefault("words", []).append((index, words[0], list(words[1])))
        env.setdefault("more", []).append((index, words[3]))
        if isinstance(lines[index], dict):
            return lines[index]
        return {"windows": [dict(w, secs=words[2]) for w in talk.heard(words[1], lines[index], secs=words[2])], "cached": False, "reused": 0,
                "cpu": 2.0, "took": 2.0}
    monkeypatch.setattr(hook, "lid_run", lid_run)


def two_audio(env, second=None):
    """english_film() with a second English audio track, a2, with the properties second."""
    t = env["probe"]["tracks"][1]
    env["probe"]["tracks"].insert(2, dict(t, id=2, properties=dict(t["properties"], uid=t["properties"]["uid"] + 50, default_track=False, **(second or {}))))
    for i, x in enumerate(env["probe"]["tracks"]):
        x["id"] = i


def kept_back(env, monkeypatch, cues):
    """An import of an English film whose default English subtitle holds cues and does not match the audio. Originals
    are kept, so a removal could run. Returns the decision record."""
    english_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": cues})
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("a track whose text does not read as its tag stays"))
    hook.main([])
    return decided(env)


def test_dutch_text_tagged_english_stays_with_its_flags_off(env, monkeypatch):
    """The English track holds Dutch text, so its words never match the English speech. Its text reads as Dutch, so it
    stays in the file with its flags off, and the alert says why."""
    rec = kept_back(env, monkeypatch, said_in(talk.cues(talk.RIGHT), DUTCH))
    s1 = rec["subcheck"]["s1"]
    assert s1["verdict"] == "mismatch" and s1["text"]["lang"] == "dut", s1
    assert rec["subremux"] == {"kept_for": {"s1": "text"}} and rec["edits"] == [["track:=10", 0, 1]], rec
    assert "subtitle_audio_mismatch" in rec["reasons"], rec["reasons"]
    (line,) = rec["findings"][0]["lines"]
    assert (line["code"], line["gone"], line["kept_back"]) == ("stays", False, "text"), line
    assert "submatch: The English subtitles (track 1) don't match what's said in the audio. They're still in the file, because their text " \
           "doesn't clearly read as English. Turned off their default and forced flags." in rec["alerts"], rec["alerts"]
    assert hook.LINE_STEPS["stays"](line) is None


@pytest.mark.parametrize("planned", [False, True])
def test_the_text_gate_names_its_reason_with_no_kept_originals(env, monkeypatch, settings, planned):
    """Reviewer F1: with KEEP_ORIGINALS_DAYS 0 a removal cannot run, but the text gate would keep the track at any
    setting. So the alert names the text, and a dry run never asks for KEEP_ORIGINALS_DAYS above 0."""
    english_film(env, ("eng", True, {}))
    hearing(env, monkeypatch, {"s1": said_in(talk.cues(talk.RIGHT), DUTCH)})
    settings(keep_days=0)
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("KEEP_ORIGINALS_DAYS 0 removes nothing"))
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--sub-check"] + ([] if planned else ["--apply"]))
    rec = [r for r in log_lines(env) if "schema" in r][-1]
    assert rec["subremux"] == {"kept_back": "keep_days", "kept_for": {"s1": "text"}}, rec.get("subremux")
    (line,) = rec["findings"][0]["lines"]
    assert (line["code"], line["kept_back"]) == ("stays", "text"), line
    flags = "--apply would turn their default and forced flags off." if planned else "Turned off their default and forced flags."
    assert rec["alerts"] == [f"submatch: The English subtitles (track 1) don't match what's said in the audio. They're still in the file, because their "
                             f"text doesn't clearly read as English. {flags}"], rec["alerts"]


@pytest.mark.parametrize("swap", [looks_alike, ocr_swapped])
def test_a_right_track_in_look_alike_letters_matches(env, monkeypatch, swap):
    """The English track holds the right lines, but its letters are Cyrillic look-alikes, all of them or in the pattern
    of a real OCR track. They read as Latin letters, so the heard words match and nothing changes."""
    english_film(env, ("eng", False, {}))
    hearing(env, monkeypatch, {"s1": swap(talk.cues(talk.RIGHT))})
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("a right track stays"))
    hook.main([])
    rec = decided(env)
    assert rec["subcheck"]["s1"]["verdict"] == "match" and not rec.get("edits") and not rec.get("alerts"), rec["subcheck"]["s1"]


def test_only_a_text_of_look_alikes_reads_as_latin():
    """The OCR swap pattern on English text reads as English, and its words are the Latin ones. Real Russian and Greek
    text keep their letters. One Cyrillic letter with no look-alike keeps the whole text as it is, so it reads as mixed
    scripts, in the first cue and in the last."""
    texts = [t for _, _, t in ocr_swapped(talk.cues(talk.RIGHT))]
    assert sum(c > "\u0400" for t in texts for c in t) > 0.1 * sum(c.isalpha() for t in texts for c in t)
    assert hook.arr_decide.text_language(texts)[0] == "eng" and hook.arr_decide.latin_lookalikes(texts) == [t for _, _, t in talk.cues(talk.RIGHT)]
    assert hook.tag_read(ocr_swapped(talk.cues(talk.RIGHT)), "eng")[0]
    assert hook.arr_decide.text_language(RUSSIAN * 20)[0] == "rus" and hook.arr_decide.latin_lookalikes(RUSSIAN) == RUSSIAN
    assert hook.arr_decide.text_language(GREEK * 20)[0] == "gre" and hook.arr_decide.latin_lookalikes(GREEK) == GREEK
    greek = [t.translate(str.maketrans("ABEHIKMNOPTXYZo", "ΑΒΕΗΙΚΜΝΟΡΤΧΥΖο")) for _, _, t in talk.cues(talk.RIGHT)]
    assert hook.arr_decide.latin_lookalikes(greek) == [t for _, _, t in talk.cues(talk.RIGHT)]   # Greek capitals and omicron
    assert hook.arr_decide.latin_lookalikes(["Тhе еnd\u0384"]) == ["The end\u0384"]   # a Greek mark is no letter
    one, last = [texts[0] + " Дa"] + texts[1:], texts[:-1] + [texts[-1] + " Дa"]
    assert hook.arr_decide.text_language(one)[2].startswith("mixed scripts"), hook.arr_decide.text_language(one)
    assert hook.arr_decide.latin_lookalikes(last) == last and hook.arr_decide.text_language(last)[2].startswith("mixed scripts")   # every cue counts
    fl = lambda cues: [w for _, _, w, _ in hook.arr_subsync.flat(cues)]
    assert fl(ocr_swapped(talk.cues(talk.RIGHT))) == fl(talk.cues(talk.RIGHT))   # the word check's cue words


def test_the_gate_the_word_check_and_the_timing_fold_by_one_rule():
    """Review F2 and N1. The text gate, the word check and the whole-file timing take the cue words of a track by one
    fold, judged on every cue, see decide.latin_lookalikes(). 1200 cues in the OCR swap with one real Cyrillic letter
    past cue 700 fold nowhere. The gate reads mixed scripts then, so a mismatch keeps the track. A track of look-alikes
    only folds in all three. Real Russian and Greek text never folds."""
    align = sys.modules[hook.tag_read.__module__.rsplit(".", 1)[0] + ".align"]
    decide, subsync = hook.arr_decide, hook.arr_subsync
    lines = [t for _, _, t in talk.cues(talk.RIGHT)]
    plain = [(30.0 + 4 * k, 33.0 + 4 * k, lines[k % len(lines)]) for k in range(1200)]
    look = ocr_swapped(plain)
    k = next(i for i in range(700, 1200) if "n" in look[i][2])
    one = look[:k] + [(look[k][0], look[k][1], look[k][2].replace("n", "п", 1))] + look[k + 1:]
    fl = lambda cues: [w for _, _, w, _ in subsync.flat(cues)]
    raw = lambda cues: [w for _, _, t in cues for w in subsync.words(t)]   # the words of the letters as they are
    heard = lambda cues: [(a + 0.2 + 0.3 * i, w) for a, _, t in cues for i, w in enumerate(subsync.tokens(t))]
    anchors = lambda cues, said: len(align.anchors(cues, heard(said), decide.STOPWORDS.get("eng", frozenset())))
    gate, read = hook.tag_read(one, "eng")
    assert not gate and read["lang"] is None and read["why"].startswith("mixed scripts"), read
    assert fl(one) == raw(one) and fl(look) == fl(plain) == raw(plain), "the word check"
    assert decide.text_language(t for _, _, t in one)[0] is None and decide.text_language(t for _, _, t in look)[0] == "eng"
    assert hook.tag_read(look, "eng") == (True, {"lang": "eng", **hook.tag_read(plain, "eng")[1]})
    assert anchors(look[:60], plain[:60]) == anchors(plain[:60], plain[:60]) > 50   # the timing reads the look-alikes as Latin
    with_real = look[:59] + [(look[59][0], look[59][1], look[59][2] + " и")]
    assert anchors(with_real, plain[:60]) < anchors(plain[:60], plain[:60]) / 2, "a real letter keeps the letters as they are"
    for text, lang in ((RUSSIAN, "rus"), (GREEK, "gre")):
        cues = [(30.0 + 4 * k, 33.0 + 4 * k, text[k % len(text)]) for k in range(60)]
        assert decide.latin_lookalikes([t for _, _, t in cues]) == [t for _, _, t in cues] and fl(cues) == raw(cues)
        assert hook.tag_read(cues, lang)[1]["lang"] == lang and anchors(cues, cues) > 50, lang
    stop = decide.STOPWORDS["eng"]
    sung = look[:60] + [(300.0, 303.0, "♪ Калинка, калинка ♪")]   # a real letter in a lyrics cue only
    assert subsync.word_times(sung, stop) == [x[0] for x in subsync.flat(sung, stop) if x[1] < 60] != subsync.word_times(plain[:60], stop)

    def then_fail(texts):   # texts, then a failure when the read asks for more
        yield from texts
        pytest.fail("the read went past its verdict")
    for texts, lang in ((RUSSIAN * 20, "rus"), (GREEK * 20, "gre"), (lines, "eng")):
        assert decide.text_language(then_fail(texts))[0] == lang   # a track with no look-alike before a real letter reads as before


@pytest.mark.parametrize("lines, lang, leaves", [
    (talk.OTHER, "eng", True),   # English text tagged English
    (DUTCH, "eng", False),       # another language in a tag with a word list
    (FINNISH, "fin", True),      # no word list holds Finnish, so its text reads as no language
    (DUTCH, "fin", False),       # Dutch text tagged Finnish reads as Dutch
    (FINNISH, "fra", False),     # fra and fre are one language, and its word list reads no French
    (["..."], "fin", False),     # no letters to judge, as a picture track has
])
def test_a_track_leaves_when_its_text_lets_it(lines, lang, leaves):
    """A track leaves for a mismatch when its text reads as its tag. The text of a tag with no word list never reads as
    that tag, so it leaves when its text reads as no other language. A text too short to judge keeps the track."""
    ok, read = hook.tag_read(said_in(talk.cues(talk.RIGHT), lines), lang)
    assert ok is leaves, read


def test_the_text_read_takes_the_first_200_cues():
    """Review N5: the read takes the blocks subtitle_read() reads, the first TEXT_CUES cues. 200 cues of music notes
    hold no letter, so English lines after them never count, and the track stays."""
    english = ["Where do you think you are going with that map tonight?", "We have to find the mine before they do.",
               "I told you the temple was under the mountain."]
    cues = said_in(talk.cues(talk.RIGHT)[:200], ["♪ ♪"]) + said_in(talk.cues(talk.RIGHT)[200:], english)
    ok, read = hook.tag_read(cues, "eng")
    assert not ok and read["lang"] is None and hook.arr_decide.text_language(t for _, _, t in cues)[0] == "eng", read


@pytest.mark.parametrize("text, leaves", [(FINNISH, True), (DUTCH, False)])
def test_a_tag_with_no_word_list_leaves_unless_it_reads_as_another_language(env, monkeypatch, text, leaves):
    """A Finnish film whose default Finnish subtitle does not match the audio. Finnish text reads as no language, so the
    track leaves. Dutch text reads as Dutch, so the track stays with its flags off."""
    monkeypatch.setattr(hook, "LANGS", [hook.arr_decide.language_table(LANGUAGES + "Finnish | fin | fin | fi\n")])
    env["movies"]["movie/7"].update(originalLanguage={"name": "Finnish"}, runtime=round(talk.DURATION / 60))
    env["probe"] = sub_probe(("fin", True, {}), audio=(("fin", True),))
    hearing(env, monkeypatch, {"s1": said_in(talk.cues(talk.RIGHT), text)})
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    got = []
    monkeypatch.setattr(hook, "resub", lambda path, j, st, apply, fixes, drop=(), **k: got.append(list(drop)) or
                        ("subtitle_remux_failed", "subtitle remux failed: a made-up fault", {"warnings": None}))
    hook.main([])
    rec = decided(env)
    assert rec["subcheck"]["s1"]["verdict"] == "mismatch", rec["subcheck"]
    assert got == ([[2]] if leaves else []) and (rec.get("subremux") or {}).get("kept_for") == (None if leaves else {"s1": "text"}), (got, rec.get("subremux"))


@pytest.mark.parametrize("shape", ["commentary", "second main"])
def test_a_subtitle_of_the_other_audio_track_stays_as_it_is(env, monkeypatch, shape):
    """An untitled subtitle holds what the second English audio track says: a titled commentary, or a second untitled
    main track. It does not match a1, which the check hears first. a2 hears the same windows and matches it, so the
    mismatch is unknown: no change, no flag edit and no alert."""
    english_film(env, ("eng", False, {}), ("eng", False, {}))
    two_audio(env, {"track_name": "Commentary", "flag_commentary": True} if shape == "commentary" else {})   # the flag, so no edit
    heard_by(env, monkeypatch, {"s1": talk.cues(talk.RIGHT), "s2": talk.cues(talk.OTHER)}, [talk.RIGHT, talk.OTHER])
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("the subtitle of the other audio track stays"))
    hook.main([])
    rec = decided(env)
    s2 = rec["subcheck"]["s2"]
    assert (s2["verdict"], s2["held"], s2["audio"]) == ("unknown", True, 0), s2
    assert s2["why"].startswith("the heard words match the cues at") and ", but on a2 the heard words match the cues at 100%, 100%" in s2["why"], s2
    assert [(o["audio"], o["verdict"]) for o in s2["others"]] == [(1, "match")] and "others" not in rec["subcheck"]["s1"], s2
    assert {k: s2["others"][0][k] for k in ("took", "cpu", "cached")} == {"took": 2.0, "cpu": 2.0, "cached": False}, s2   # review N6
    first = [w for i, _, w in env["words"] if i == 0][0]
    assert [w for i, _, w in env["words"] if i == 1] == [first], env["words"]   # the windows of the first hearing
    more = [m for i, m in env["more"] if i == 0][0]
    assert more and [m for i, m in env["more"] if i == 1] == [more], env["more"]   # and its longer windows
    assert not rec.get("subremux") and not rec.get("edits") and not rec.get("alert_kinds"), rec


def test_a_second_hearing_with_no_answer_keeps_the_subtitle(env, monkeypatch):
    """a2 gives no answer, so nothing rules out that the subtitle follows it. The mismatch is unknown and the track
    stays as it is."""
    english_film(env, ("eng", False, {}))
    two_audio(env, {"track_name": "Commentary", "flag_commentary": True})   # the flag, so no edit
    heard_by(env, monkeypatch, {"s1": talk.cues(talk.OTHER)}, [talk.RIGHT, {"why": "no answer in 75 seconds"}])
    monkeypatch.setattr(hook, "CFG", dataclasses.replace(hook.CFG, keep_days=7))
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("a subtitle no hearing ruled out stays"))
    hook.main([])
    rec = decided(env)
    s1 = rec["subcheck"]["s1"]
    assert (s1["verdict"], s1["held"]) == ("unknown", True) and s1["why"].endswith(", but a2 is in its language too and could not be heard: "
                                                                                   "no answer in 75 seconds"), s1
    assert not rec.get("subremux") and not rec.get("edits") and not rec.get("alert_kinds"), rec


def test_a_mismatch_that_the_other_track_does_not_match_counts(env, monkeypatch):
    """The subtitle holds another episode's lines, and neither English audio track speaks them. It leaves the file."""
    got = removal_film(env, monkeypatch, None)
    two_audio(env, {"track_name": "Commentary"})
    heard_by(env, monkeypatch, {"s1": talk.cues(talk.OTHER), "s2": talk.cues(talk.RIGHT)}, [talk.RIGHT, talk.RIGHT])
    hook.main([])
    rec = decided(env)
    assert got == [(True, {}, [3])] and rec["subremux"]["removed"] == ["s1"], (got, rec.get("subremux"))
    assert [(o["audio"], o["verdict"]) for o in rec["subcheck"]["s1"]["others"]] == [(1, "mismatch")], rec["subcheck"]["s1"]


def test_a_removal_names_the_file_of_its_text(env, monkeypatch):
    """The remux names the file that holds the removed track's text, and the alert says where it is."""
    removal_film(env, monkeypatch, None)
    fake = hook.resub
    monkeypatch.setattr(hook, "resub", lambda *a, **k: (lambda r: (*r[:2], dict(r[2], mismatch_text={"2": "F.eng.mismatch.srt.txt"})))(fake(*a, **k)))
    hook.main([])
    rec = decided(env)
    assert rec["subremux"]["removed"] == ["s1"] and rec["subremux"]["saved"] == {"s1": "F.eng.mismatch.srt.txt"}, rec["subremux"]
    assert rec["alerts"] == ["submatch: The English subtitles (track 1) don't match what's said in the audio. Removed them and kept the original file "
                             "at /kept/f.mkv. Saved their text beside the file as F.eng.mismatch.srt.txt."], rec["alerts"]


def test_a_failed_removal_names_no_file_of_its_text(env, monkeypatch):
    """Review N2: resub() names the file of the text at its plan, and a failed remux writes none, or removes it again.
    So the record and the alert name no file."""
    removal_film(env, monkeypatch, None, fails=True)
    fake = hook.resub
    monkeypatch.setattr(hook, "resub", lambda *a, **k: (lambda r: (*r[:2], dict(r[2], mismatch_text={"2": "F.eng.mismatch.srt.txt"})))(fake(*a, **k)))
    hook.main([])
    rec = decided(env)
    assert rec["subremux"]["codes"] == ["subtitle_remux_failed"] and "saved" not in rec["subremux"], rec["subremux"]
    (line,) = rec["findings"][0]["lines"]
    assert (line["code"], line["gone"]) == ("stays", True) and "saved" not in line, line


def test_no_time_left_for_the_second_hearing_gives_unknown(monkeypatch):
    """The check's time is spent, so a3 gets no hearing, and the mismatch is unknown. a2 is French, so it never hears
    for an English subtitle."""
    j = {"container": {"properties": {"duration": int(talk.DURATION * 1e9)}}, "tracks": []}
    ts = [{"kind": "a", "lang": "eng", "pos": "a1"}, {"kind": "a", "lang": "fre", "pos": "a2"}, {"kind": "a", "lang": "eng", "pos": "a3"}]
    items = {"s1": ("eng", 0, talk.cues(talk.OTHER))}
    res = {"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 3%, 5% at best", "starts": [[[150.0, 950.0], 10.0]]}}
    monkeypatch.setattr(hook, "SUB_TIMEOUT", 5)
    monkeypatch.setattr(hook, "lid_run", lambda *a, **k: pytest.fail("no time left to hear"))
    got = hook.sub_others("/m/F.mkv", j, res, items, ts)["s1"]
    assert (got["verdict"], got["held"]) == ("unknown", True) and got["why"].endswith(", but a3 is in its language too and could not be heard: "
                                                                                      "no time left of the 5 seconds the check may take"), got
    assert [o["audio"] for o in got["others"]] == [2], got


def test_a_name_taken_beside_the_video_keeps_the_track(env, monkeypatch):
    """Files of other bytes hold both names for the text of the removed track, so it stays with its flags off."""
    removal_film(env, monkeypatch, None)
    base = os.path.splitext(env["path"])[0]
    for name in (f"{base}.eng.mismatch.srt.txt", f"{base}.eng.1.mismatch.srt.txt"):
        with open(name, "w") as f:
            f.write("other bytes")
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("no remux"))
    hook.main([])
    rec = decided(env)
    assert rec["subremux"] == {"kept_for": {"s1": "names"}} and rec["edits"] == [["track:=10", 0, 1]], rec
    (line,) = rec["findings"][0]["lines"]
    assert (line["code"], line["kept_back"]) == ("stays", "names") and hook.LINE_STEPS["stays"](line) == "Writing the file", line   # review N1


def test_convert_subs_keeps_a_track_whose_text_reads_as_another_language(monkeypatch, settings, tmp_path):
    """A conversion: the built-in English track holds Dutch text and does not match. It stays in the remux, and the
    check of the new Matroska file turns its flags off."""
    video = tmp_path / "Film A (1979).mp4"
    video.write_bytes(b"x")
    j = sub_probe(("eng", False, {}), audio=(("eng", True),))
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}, {"index": 1, "codec_type": "audio", "codec_name": "aac"},
               {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text"}]
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    settings(keep_days=7)
    stamp = lambda s: f"0:{int(s // 60):02d}:{int(s % 60):02d},000"
    text = "".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(said_in(talk.cues(talk.RIGHT), DUTCH)[:40], 1))
    monkeypatch.setattr(hook.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=text, stderr=""))
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, starts=None, line=True, deep=False, streams=None:
                        {"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 0%, 3% at best", "timing": None}})
    info = {}
    keep, drop = hook.convert_subs(str(video), j, streams, [], talk.DURATION, True, str(tmp_path), info)
    assert drop == [] and "tracks_unmatched" not in info and info["subcheck"]["s1"]["text"]["lang"] == "dut", info


@pytest.fixture(scope="module")
def two_subs(tmp_path_factory):
    """A made-up 30 s file with H.264 video, AAC audio and two English SubRip tracks."""
    d = tmp_path_factory.mktemp("subs")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=30:size=160x90:rate=25", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=30", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", str(d / "av.mkv")], check=True)
    (d / "a.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nWhere is the map?\n\n2\n00:00:20,000 --> 00:00:21,500\nUnder the bridge.\n\n")
    (d / "b.srt").write_text("1\n00:00:03,000 --> 00:00:04,000\nThe lines of another film.\n\n")
    REAL_RUN(["mkvmerge", "-q", "-o", str(d / "f.mkv"), str(d / "av.mkv"), "--language", "0:en", str(d / "a.srt"), "--language", "0:en",
              str(d / "b.srt")], check=True)
    return d


@pytest.mark.parametrize("apply", [False, True])
def test_a_removed_track_keeps_its_text_beside_the_video(two_subs, tmp_path, settings, monkeypatch, apply):
    """The remux drops the first English track. Its SubRip text goes beside the video as f.eng.mismatch.srt.txt, after
    the proof and the kept original. A dry run names the file and writes none."""
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    path = str(tmp_path / "f.mkv")
    shutil.copy(two_subs / "f.mkv", path)
    j = REAL_MKVMERGE(path)
    code, result, info = hook.resub(path, j, os.stat(path), apply, {}, drop=[2])
    side = tmp_path / "f.eng.mismatch.srt.txt"
    assert info["mismatch_text"] == {"2": side.name} and "garbled_text" not in info, info
    if not apply:
        assert (code, result) == ("would_remux_subtitles", f"would remux subtitles: remove track 2, its text to {side.name}") and not side.exists()
        return
    assert code == "subtitles_remuxed" and info.get("kept"), (result, info)
    assert side.read_text() == "1\n00:00:01,000 --> 00:00:02,000\nWhere is the map?\n\n2\n00:00:20,000 --> 00:00:21,500\nUnder the bridge.\n\n"
    assert [t["type"] for t in REAL_MKVMERGE(path)["tracks"]] == ["video", "audio", "subtitles"]


@pytest.fixture(scope="module")
def two_timed(tmp_path_factory):
    """A made-up 20 s MP4 with H.264 video, AAC audio and two English timed-text tracks."""
    d = tmp_path_factory.mktemp("mp4")
    (d / "a.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nWhere is the map?\n\n2\n00:00:12,000 --> 00:00:13,500\nUnder the bridge.\n\n")
    (d / "b.srt").write_text("1\n00:00:03,000 --> 00:00:04,000\nThe lines of another film.\n\n")
    amg.ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=20:size=160x90:rate=25", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=20", "-i", str(d / "a.srt"), "-i", str(d / "b.srt"), "-map", "0", "-map", "1", "-map", "2", "-map", "3",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
                "-metadata:s:s:1", "language=eng", str(d / "f.mp4")], check=True)
    return d


def converted(two_timed, tmp_path, settings, monkeypatch, apply, taken=(), more=None):
    """convert() of a copy of the MP4 of two_timed, a file no app lists, whose subtitle check leaves out s1, and adds
    more to info. taken are names that hold a file beside it. Returns (code, result, info, the new file's path)."""
    settings(keep_days=7, log=str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    path = tmp_path / "f.mp4"
    shutil.copy(two_timed / "f.mp4", path)
    for name in taken:
        (tmp_path / name).write_text("other bytes")

    def left_out(path, j, streams, subs, dur, apply, folder, info, known=(), broken=(), ready=None):
        info.update(subcheck={"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 5%, 8% at best"}}, tracks_unmatched=["s1"],
                    **(more or {}))
        sub = [t["id"] for t in j["tracks"] if t["type"] == "subtitles"][0]
        return subs, [(sub, next(x["index"] for x in streams if x.get("codec_type") == "subtitle"))]
    monkeypatch.setattr(hook, "convert_subs", left_out)
    return hook.convert("radarr", str(path), REAL_MKVMERGE(str(path)), os.stat(path), apply, {}, outside=True, source="check", sub=True)


@pytest.mark.parametrize("apply", [False, True])
def test_a_conversion_keeps_the_text_of_a_track_it_leaves_out(two_timed, tmp_path, settings, monkeypatch, apply):
    """The conversion leaves out the first English track, and its text goes beside the new file as
    f.eng.mismatch.srt.txt, read at the bytes of its samples. A dry run names the file and writes none."""
    code, result, info, new = converted(two_timed, tmp_path, settings, monkeypatch, apply)
    side = tmp_path / "f.eng.mismatch.srt.txt"
    assert info["mismatch_text"] == {"s1": side.name} and info["tracks_unmatched"] == ["s1"], info
    if not apply:
        assert code == "would_repack" and f"take out subtitle track 1, its text to {side.name}" in result and not side.exists(), result
        return
    assert code == "repacked", (result, info)
    # The times move by the start shift of the new file, AAC priming here, as for a broken track, see start_shift()
    assert re.fullmatch(r"1\n00:00:01,0\d\d --> 00:00:02,0\d\d\nWhere is the map\?\n\n2\n00:00:12,0\d\d --> 00:00:13,5\d\d\nUnder the bridge\.\n\n",
                        side.read_text()), side.read_text()
    assert [t["type"] for t in REAL_MKVMERGE(new)["tracks"]] == ["video", "audio", "subtitles"]


def test_a_taken_name_keeps_the_track_in_the_conversion(two_timed, tmp_path, settings, monkeypatch):
    """Files of other bytes hold both names for the text, so the track stays in the new file. The check of the new
    file then judges it again, see subtitle_checks()."""
    code, result, info, new = converted(two_timed, tmp_path, settings, monkeypatch, True, taken=("f.eng.mismatch.srt.txt", "f.eng.1.mismatch.srt.txt"))
    assert code == "repacked" and info["mismatch_stayed"] == ["s1"] and "tracks_unmatched" not in info and "mismatch_text" not in info, info
    assert [t["type"] for t in REAL_MKVMERGE(new)["tracks"]] == ["video", "audio", "subtitles", "subtitles"]


def test_a_track_with_no_timed_text_stays_in_the_conversion():
    """Only an MP4 timed-text track has its bytes read in a conversion. A WebVTT track stays, with no read, and the
    remux of the new file keeps its bytes."""
    info = {"tracks_unmatched": ["s1"]}
    drop, texts = hook.mismatch_texts("/nowhere/F.webm", {"tracks": [{"id": 2, "type": "subtitles", "properties": {"language": "eng"}}]},
                                      [{"index": 2, "codec_type": "subtitle", "codec_name": "webvtt"}], [(2, 2)], info)
    assert (drop, texts) == ([], {}) and info == {"mismatch_stayed": ["s1"]}, info


SIDE_NAMES = [".en.mismatch.srt.txt"] + [f".en.{n}.mismatch.srt.txt" for n in range(1, 100)]   # every name of an English sidecar's bytes


def test_a_sidecar_whose_names_are_taken_moves_to_the_kept_originals(env, monkeypatch, tmp_path):
    """Files of other bytes hold every name for the sidecar's bytes beside the video, the plain one and numbers 1 to 99,
    so it moves to the kept originals, as before the rename."""
    sidecar_film(env, monkeypatch, tmp_path, _en_srt=talk.cues(talk.OTHER))
    for end in SIDE_NAMES:
        with open(env["path"][:-4] + end, "w") as f:
            f.write("other bytes")
    hook.main([])
    (e,) = decided(env)["sidecars"]
    assert e["result"] == "moved" and "to" not in e and e["kept"].startswith(str(tmp_path / ".kept")), e
    assert not os.path.exists(env["path"][:-4] + ".en.srt")


@pytest.mark.parametrize("keep_days", [7, 0])
def test_a_dry_run_names_the_new_name_of_a_sidecar(tmp_path, settings, keep_days):
    """A dry run plans the name of a sidecar that does not match and leaves it as it is. With KEEP_ORIGINALS_DAYS 0
    the sidecar stays, so the dry run plans no name (review G1)."""
    settings(keep_days=keep_days)
    video = tmp_path / "Film.mkv"
    video.write_bytes(b"x")
    (tmp_path / "Film.en.srt").write_text(srt_text(talk.cues(talk.OTHER)))
    sides = {s["name"]: s for s in hook.sidecar_subs(str(video))}
    (e,) = hook.sidecar_fix(sides, {"Film.en.srt": {"verdict": "mismatch", "why": "5%"}}, False, "radarr", "sub_time", video=str(video))
    assert (e["action"], e["result"], e.get("to")) == ("move", "dry run", "Film.en.mismatch.srt.txt" if keep_days else None), e
    assert (tmp_path / "Film.en.srt").exists() and not (tmp_path / "Film.en.mismatch.srt.txt").exists()


@pytest.mark.parametrize("apply", [False, True])
def test_a_sidecar_gone_since_the_check_stays_left(tmp_path, settings, apply):
    """Review G2: a program such as Bazarr replaced the sidecar during the run, so its file is gone at the plan. The run
    goes on, and the sidecar gives result left with its reason, as the move does, in a dry run too (review N8)."""
    settings(keep_days=7)
    video = tmp_path / "Film.mkv"
    video.write_bytes(b"x")
    (tmp_path / "Film.en.srt").write_text(srt_text(talk.cues(talk.OTHER)))
    sides = {s["name"]: s for s in hook.sidecar_subs(str(video))}
    (tmp_path / "Film.en.srt").unlink()
    (e,) = hook.sidecar_fix(sides, {"Film.en.srt": {"verdict": "mismatch", "why": "5%"}}, apply, "radarr", "sub_time", video=str(video))
    assert "to" not in e and e["result"] == "left" and "FileNotFoundError" in e["error"], e


@pytest.mark.parametrize("taken", [False, True, "keep_days 0"])
def test_a_conversion_renames_a_sidecar_that_does_not_match(tmp_path, settings, monkeypatch, taken):
    """A conversion leaves out a sidecar that does not match, and gives it a name no player reads beside the file. With
    every name taken it moves to the kept originals. With KEEP_ORIGINALS_DAYS 0 it stays, and no name is planned
    (review G1)."""
    video = tmp_path / "Film.mp4"
    video.write_bytes(b"x")
    (tmp_path / "Film.en.srt").write_text(srt_text(talk.cues(talk.OTHER)))
    for end in SIDE_NAMES if taken is True else ():
        (tmp_path / f"Film{end}").write_text("other bytes")
    settings(keep_days=0 if taken == "keep_days 0" else 7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, *a, **k: {"Film.en.srt": {"verdict": "mismatch", "why": "5%", "timing": None}})
    info = {}
    keep, drop = hook.convert_subs(str(video), sub_probe(audio=(("eng", True),)), [], hook.sidecar_subs(str(video)), talk.DURATION, True, str(tmp_path), info)
    (entry,) = info["sidecars_unmatched"]
    if taken == "keep_days 0":
        assert entry == {"name": "Film.en.srt", "why": "5%", "left": "KEEP_ORIGINALS_DAYS is 0, so the original could not be kept"} and (tmp_path / "Film.en.srt").exists(), entry
        return
    assert keep == [] and drop == [] and not (tmp_path / "Film.en.srt").exists(), info
    if taken:
        assert entry["moved"].startswith(str(tmp_path / ".kept")) and "to" not in entry, entry
    else:
        assert entry == {"name": "Film.en.srt", "why": "5%", "to": "Film.en.mismatch.srt.txt", "renamed": True}, entry
        assert (tmp_path / "Film.en.mismatch.srt.txt").read_text() == srt_text(talk.cues(talk.OTHER))


def others_run(monkeypatch, langs, answer, step=0.0):
    """sub_others() of a made-up English subtitle of another episode, compared with a1, in a file with the audio
    languages langs. The hearing of audio index i gives answer(i), and each hearing moves a fake clock by step
    seconds. Returns (the result, the audio indexes heard)."""
    clock, asked = [0.0], []
    mod = sys.modules[hook.sub_others.__module__]
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    def lid_run(path, index, j, expect, timeout, words=None, **k):
        asked.append(index)
        clock[0] += step
        return answer(index, words)
    monkeypatch.setattr(hook, "lid_run", lid_run)
    j = {"container": {"properties": {"duration": int(talk.DURATION * 1e9)}}, "tracks": []}
    ts = [{"kind": "a", "lang": x, "pos": f"a{n}"} for n, x in enumerate(langs, 1)]
    res = {"s1": {"verdict": "mismatch", "why": "the heard words match the cues at 3%, 5% at best", "starts": [[[talk.EARLY, talk.LATE], 10.0]]}}
    return hook.sub_others("/m/F.mkv", j, res, {"s1": ("eng", 0, talk.cues(talk.OTHER))}, ts)["s1"], asked


def heard(lines):
    """A hearing that hears lines in the windows asked."""
    return lambda i, words: {"windows": [dict(w, secs=words[2]) for w in talk.heard(words[1], lines, secs=words[2])], "cpu": 1.0, "took": 1.0}


@pytest.mark.parametrize("convert", [False, True])
def test_a_move_whose_remove_fails_still_names_the_kept_copy(tmp_path, settings, monkeypatch, convert):
    """Review R1: every name beside the video is taken, so a sidecar that does not match moves to the kept originals.
    Its remove fails after the keep. The sidecar stays, and its record names the kept copy, as kept beside an MKV and as
    moved in a conversion."""
    video, side = tmp_path / ("Film.mp4" if convert else "Film.mkv"), tmp_path / "Film.en.srt"
    video.write_bytes(b"x")
    side.write_text(srt_text(talk.cues(talk.OTHER)))
    for end in SIDE_NAMES:
        (tmp_path / f"Film{end}").write_text("other bytes")
    settings(keep_days=7, log=str(tmp_path / "log.jsonl"), state_dir=str(tmp_path))
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    real = os.remove
    monkeypatch.setattr(os, "remove", lambda p, *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied", p))
                        if str(p) == str(side) else real(p, *a, **k))
    if convert:
        monkeypatch.setattr(hook, "lid_ready", lambda: True)
        monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, *a, **k: {"Film.en.srt": {"verdict": "mismatch", "why": "5%", "timing": None}})
        info = {}
        hook.convert_subs(str(video), sub_probe(audio=(("eng", True),)), [], hook.sidecar_subs(str(video)), talk.DURATION, True, str(tmp_path), info)
        (e,) = info["sidecars_unmatched"]
        kept = e.get("moved")
    else:
        sides = {s["name"]: s for s in hook.sidecar_subs(str(video))}
        (e,) = hook.sidecar_fix(sides, {side.name: {"verdict": "mismatch", "why": "no match"}}, True, "radarr", "sub_time", video=str(video))
        assert e["result"] == "left", e
        kept = e.get("kept")
    assert kept and os.path.exists(kept) and "left" in e and side.exists(), e


@pytest.mark.parametrize("captions", [False, True])
def test_a_conversion_checks_a_text_track_beside_a_caption_stream(monkeypatch, captions):
    """mkvmerge lists no track for a CEA-608 caption stream, so the conversion pairs the ffprobe subtitle streams
    without it, see convert.sub_streams(). The text track of an MP4 then gets its subtitle check with a caption stream
    too."""
    checked = []
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "run_bounded", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="1\n00:00:01,000 --> 00:00:02,000\nHello there\n"))
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, *a, **k: checked.append(sorted(items)) or {})
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}, {"index": 1, "codec_type": "audio", "codec_name": "aac"},
               {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text"}] + [{"index": 3, "codec_type": "subtitle", "codec_name": "eia_608"}] * captions
    hook.convert_subs("/x/Film.mp4", sub_probe(("eng", True, {}), audio=(("eng", True),)), streams, [], talk.DURATION, False, "/x", {})
    assert checked == [["s1"]], checked


def test_the_other_tracks_share_the_time_of_one_check(monkeypatch):
    """Reviewer F2: each hearing takes 45 s of the check's 90, so a2 and a3 hear and a4 gets no time. The mismatch is
    then unknown, as a4 may be the audio the subtitle follows."""
    got, asked = others_run(monkeypatch, ["eng"] * 4, heard(talk.RIGHT), step=45.0)
    assert asked == [1, 2] and [(o["audio"], o["verdict"]) for o in got["others"]] == [(1, "mismatch"), (2, "mismatch"), (3, "unknown")], got
    assert (got["verdict"], got["held"]) == ("unknown", True) and got["why"].endswith(
        ", but a4 is in its language too and could not be heard: no time left of the 90 seconds the check may take"), got


def test_no_track_hears_after_a_match(monkeypatch):
    """Reviewer F2: a2 matches, so the subtitle stays, and a3 never hears."""
    got, asked = others_run(monkeypatch, ["eng"] * 3, heard(talk.OTHER))
    assert asked == [1] and [(o["audio"], o["verdict"]) for o in got["others"]] == [(1, "match")] and got["verdict"] == "unknown", got


def test_a_stop_leaves_the_second_hearing_out(monkeypatch):
    """Review N4: a backfill apply holds a stop, so SIGTERM only asks. No further hearing starts then, and the track
    stays as it is."""
    stop = sys.modules[hook.sub_others.__module__.rsplit(".", 1)[0] + ".runner"].STOP
    monkeypatch.setitem(stop, "asked", True)
    monkeypatch.setitem(stop, "skipped", False)
    got, asked = others_run(monkeypatch, ["eng"] * 3, lambda i, words: pytest.fail("no hearing after a stop"))
    assert asked == [] and (got["verdict"], got["held"]) == ("unknown", True) and stop["skipped"], got
    assert got["why"].endswith(", but a2 is in its language too and could not be heard: the run was stopped"), got


def test_a_dry_run_names_one_file_once_for_a_track_and_a_sidecar(env, monkeypatch):
    """Review N7: an English track and the sidecar Film.eng.srt both do not match. The track plans
    <base>.eng.mismatch.srt.txt, so the sidecar takes the next number in the dry run too, as in an apply, where the
    track's file is written first."""
    removal_film(env, monkeypatch, None)
    base = env["path"][:-4]
    with open(base + ".eng.srt", "w") as f:
        f.write(srt_text(talk.cues(talk.OTHER)))
    track = os.path.basename(base) + ".eng.mismatch.srt.txt"
    monkeypatch.setattr(hook, "resub", lambda *a, **k: ("would_remux_subtitles", "would remux subtitles: remove track 2", {"mismatch_text": {"2": track}}))
    env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
    monkeypatch.setattr(hook.os, "nice", lambda n: None)
    hook.main(["--backfill", "radarr", "--sub-check"])
    rec = [r for r in log_lines(env) if "schema" in r][-1]
    (e,) = rec["sidecars"]
    assert rec["subremux"]["saved"] == {"s1": track} and (e["result"], e["to"]) == ("dry run", os.path.basename(base) + ".eng.1.mismatch.srt.txt"), rec


def test_a_conversion_dry_run_names_one_file_once_for_a_sidecar_and_a_track(two_timed, tmp_path, settings, monkeypatch):
    """Review N7: in a conversion the sidecar plans first. Its name counts as taken for the track left out, so the
    track's text takes the next number in the dry run."""
    side = {"sidecars_unmatched": [{"name": "f.eng.srt", "why": "5%", "to": "f.eng.mismatch.srt.txt"}]}   # as convert_subs() plans it
    code, result, info, new = converted(two_timed, tmp_path, settings, monkeypatch, False, more=side)
    assert code == "would_repack" and info["mismatch_text"] == {"s1": "f.eng.1.mismatch.srt.txt"}, info


def test_a_conversion_track_stays_when_a_sidecar_and_a_file_hold_its_names(two_timed, tmp_path, settings, monkeypatch):
    """The dry run gave the sidecar f.eng.mismatch.srt.txt, and a file of other bytes holds f.eng.1.mismatch.srt.txt.
    The track left out then has no name, so it stays in the conversion, which still runs."""
    side = {"sidecars_unmatched": [{"name": "f.eng.srt", "why": "5%", "to": "f.eng.mismatch.srt.txt"}]}
    code, result, info, new = converted(two_timed, tmp_path, settings, monkeypatch, False, taken=("f.eng.1.mismatch.srt.txt",), more=side)
    assert code == "would_repack" and info["mismatch_stayed"] == ["s1"] and "mismatch_text" not in info, (result, info)


def test_two_sidecars_of_one_language_get_two_names_in_a_dry_run(tmp_path, settings):
    """Review H1: Film.en.srt and Film.en.hi.srt both do not match. The dry run names the second .1., as the apply does,
    where the first file is written before the second plans."""
    settings(keep_days=7)
    video = tmp_path / "Film.mkv"
    video.write_bytes(b"x")
    for name in ("Film.en.srt", "Film.en.hi.srt"):
        (tmp_path / name).write_text(srt_text(talk.cues(talk.OTHER)))
    sides = {s["name"]: s for s in hook.sidecar_subs(str(video))}
    sync = {n: {"verdict": "mismatch", "why": "5%"} for n in sides}
    plans = [(e["name"], e["to"]) for e in hook.sidecar_fix(sides, sync, False, "radarr", "sub_time", video=str(video))]
    done = [(e["name"], e["to"]) for e in hook.sidecar_fix(sides, sync, True, "radarr", "sub_time", video=str(video))]
    assert plans == done == [("Film.en.hi.srt", "Film.en.mismatch.srt.txt"), ("Film.en.srt", "Film.en.1.mismatch.srt.txt")], (plans, done)


def test_two_sidecars_of_one_language_get_two_names_in_a_conversion_dry_run(tmp_path, settings, monkeypatch):
    """Review H1, in a conversion: the second sidecar of one language takes the next number in the dry run."""
    video = tmp_path / "Film.mp4"
    video.write_bytes(b"x")
    for name in ("Film.en.srt", "Film.en.hi.srt"):
        (tmp_path / name).write_text(srt_text(talk.cues(talk.OTHER)))
    settings(keep_days=7)
    monkeypatch.setattr(hook, "originals_root", lambda p: str(tmp_path / ".kept"))
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, *a, **k: {n: {"verdict": "mismatch", "why": "5%", "timing": None} for n in items})
    info = {}
    hook.convert_subs(str(video), sub_probe(audio=(("eng", True),)), [], hook.sidecar_subs(str(video)), talk.DURATION, False, str(tmp_path), info)
    assert [(e["name"], e["to"]) for e in info["sidecars_unmatched"]] == [("Film.en.hi.srt", "Film.en.mismatch.srt.txt"),
                                                                           ("Film.en.srt", "Film.en.1.mismatch.srt.txt")], info


@pytest.mark.parametrize("apply", [False, True])
def test_a_keep_folder_that_cannot_be_made_leaves_a_sidecar_in_both_runs(tmp_path, settings, monkeypatch, apply):
    """Review N8: the keep folder cannot be made, so the apply leaves the sidecar. The dry run asks the same, so it
    names that reason in place of a rename the apply never makes, beside an MKV and in a conversion."""
    settings(keep_days=7)
    monkeypatch.setattr(hook, "keepable", lambda path, st: "/m/.kept is not writable")
    video = tmp_path / "Film.mkv"
    video.write_bytes(b"x")
    (tmp_path / "Film.en.srt").write_text(srt_text(talk.cues(talk.OTHER)))
    sides = {s["name"]: s for s in hook.sidecar_subs(str(video))}
    (e,) = hook.sidecar_fix(sides, {"Film.en.srt": {"verdict": "mismatch", "why": "5%"}}, apply, "radarr", "sub_time", video=str(video))
    assert (e["result"], e["left"]) == ("left", "/m/.kept is not writable") and (tmp_path / "Film.en.srt").exists(), e
    mp4 = tmp_path / "Film.mp4"
    mp4.write_bytes(b"x")
    monkeypatch.setattr(hook, "lid_ready", lambda: True)
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, *a, **k: {n: {"verdict": "mismatch", "why": "5%", "timing": None} for n in items})
    info = {}
    hook.convert_subs(str(mp4), sub_probe(audio=(("eng", True),)), [], hook.sidecar_subs(str(mp4)), talk.DURATION, apply, str(tmp_path), info)
    (entry,) = info["sidecars_unmatched"]
    assert entry["left"] == "/m/.kept is not writable" and (tmp_path / "Film.en.srt").exists(), entry


@pytest.mark.parametrize("default, ietf", [(True, None), (False, None), (False, "en-us")])
@pytest.mark.parametrize("planned", [False, True])
def test_the_flags_sentence_names_only_a_flag_the_run_edits(env, monkeypatch, settings, default, ietf, planned):
    """A track that stays says its flags go off only when the run turns one off. A track with neither flag on gets no
    flag edit and no sentence on its flags, in an import and in a dry run. An edit of its language tag form is no flag
    edit."""
    english_film(env, ("eng", default, {"language_ietf": ietf} if ietf else {}))
    hearing(env, monkeypatch, {"s1": said_in(talk.cues(talk.RIGHT), DUTCH)})
    settings(keep_days=7)
    monkeypatch.setattr(hook, "resub", lambda *a, **k: pytest.fail("a track whose text does not read as its tag stays"))
    if planned:
        env["movies"]["movie"] = [dict(env["movies"]["movie/7"], id=7, movieFile={"id": 11, "path": env["path"]})]
        monkeypatch.setattr(hook.os, "nice", lambda n: None)
        hook.main(["--backfill", "radarr", "--sub-check"])
        rec = [r for r in log_lines(env) if "schema" in r][-1]
    else:
        hook.main([])
        rec = decided(env)
    flags = (" --apply would turn their default and forced flags off." if planned else " Turned off their default and forced flags.") if default else ""
    props = [hook.arr_decide.prop(e) for e in rec.get("edits") or []]
    assert (props.count("flag-default"), "language" in props) == (int(default), bool(ietf)), rec.get("edits")
    assert rec["alerts"] == ["submatch: The English subtitles (track 1) don't match what's said in the audio. They're still in the file, because "
                             f"their text doesn't clearly read as English.{flags}"], rec["alerts"]


@pytest.mark.parametrize("run, posts", [("hook", "issues"), ("hook", "all"), ("check", "issues")])
@pytest.mark.parametrize("default", [True, False])
def test_a_deep_alert_names_the_flags_its_import_turned_off(env, monkeypatch, settings, run, posts, default):
    """The import calls the English track a mismatch, keeps it with no original kept, and holds its alert for the deep
    analysis, which finds the mismatch again. Its alert takes the place of the import's, so it says the flags went off
    when the import turned them off, see logs.alert_findings(). A track that had no flag on gets no such sentence. With
    DISCORD_POSTS all the import's flag change then posts nothing of its own. --check --apply runs both at once."""
    from test_arr_media_guard import REF, block_stand_ins, check_listed, checked, posted, sub_time_film, two_lines
    settings(subtitles="deep", discord_posts=posts)
    sub_time_film(env, monkeypatch, [("eng", default, {})], {"s1": REF})
    wrong = {"verdict": "mismatch", "why": "the heard words match the cues at 5%, 8% at best", "windows": [], "timing": None}
    monkeypatch.setattr(hook, "sub_verdicts", lambda path, j, items, starts=None, line=True, deep=False, streams=None: {
        k: dict(wrong, audio=0, starts=[]) for k in items if k == "s1"})
    block_stand_ins(env, monkeypatch, found=())
    if run == "check":
        check_listed(env, monkeypatch)
        hook.main(["--check", env["path"], "--apply"])
        first, deep = checked(env)
    else:
        hook.main([])
        first, (deep,) = two_lines(env)
    assert bool(first.get("edits")) == default and first["alert_result"] == [hook.HOLD_RESULT], first
    (alert,) = deep["alerts"]
    assert alert.endswith("because KEEP_ORIGINALS_DAYS is 0, and a removal needs a copy of the original." +
                          " Turned off their default and forced flags." * default), alert
    said = "checked again, change said by the deep analysis" if default and posts == "all" else "checked again"
    assert deep["held_result"] == [said] and not any("default flag" in d for _, d in posted(env)), (deep["held_result"], posted(env))
