# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""Default-track decisions and file checks for arr-media-guard. No I/O, so the hook, the backfill, the audit
and the tests share one copy. Input is mkvmerge -J or ffprobe JSON plus the item's original language as Sonarr or
Radarr names it ("English", "Japanese", "Portuguese (Brazil)").

A decision has four steps. Each step reads general signals, so a new case needs data, not a new branch.
1. classify() reads each audio and subtitle track once. A track gets a language with a confidence, a role and its
   subtitle events a minute. The tag and a language the track title names are cross-checked. A conflict lowers the
   confidence, and the title wins ("French" tagged eng is French). An untagged track takes its title's language.
   The language lid.py heard on an audio track, when the hook passes one, confirms the language or replaces it.
2. decide() puts the item in a class: English original, foreign original, or foreign kids title. POLICY["audio"]
   lists the audio languages to try for each class. The original language from the app is checked against the
   file. When no track is in it, English plays only when its language is certain. POLICY["subtitles"] then gives
   the English subtitle roles a viewer of that audio needs.
3. On conflicting signals decide() abstains. The plan is empty and "undecided" names the reason.
4. invariants() checks the final state of every plan. A plan that breaks one is dropped, and "dropped" says why.

An edit is [selector, new, old] for a default flag. An edit that clears a forced flag carries FORCED_FLAG as a fourth item,
and an edit of the Original language flag ORIGINAL_FLAG, see retag().

POLICY is the policy file (POLICY_FILE), JSON. The hook passes it to set_policy() at start.
docs/design.md explains the rules behind it.
"""
import codecs
import re
import statistics
import unicodedata

ALIAS = [{"jpn", "jap", "japanese"}, {"ger", "deu", "german"}, {"fre", "fra", "french"}, {"chi", "zho", "cmn", "yue", "chinese", "mandarin", "cantonese"},
         {"dut", "nld", "dutch", "flemish"}, {"cze", "ces", "czech"}, {"gre", "ell", "greek"}, {"rum", "ron", "romanian"}, {"per", "fas", "persian"},
         {"nor", "nob", "nno", "norwegian"}, {"ice", "isl", "icelandic"}, {"may", "msa", "malay"}, {"wel", "cym", "welsh"}, {"slo", "slk", "slovak"},
         {"slv", "slovenian"}, {"srp", "serbian"}, {"hrv", "croatian"}, {"pan", "punjabi"}, {"tgl", "fil", "tagalog", "filipino"}, {"glg", "galician"},
         {"alb", "sqi", "albanian"}, {"arm", "hye", "armenian"}, {"baq", "eus", "basque"}, {"geo", "kat", "georgian"}, {"mac", "mkd", "macedonian"},
         {"por", "pob", "portuguese"}, {"lav", "latvian"}, {"gle", "irish"}, {"mlt", "maltese"}]
UNTAGGED = {"und", "mul", "zxx", ""}
# Language words a track title may name. A word followed by Dub, Score or Sub names something else: "UK Dub / Japanese Score".
LANGWORDS = {"english": "eng", "anglais": "eng", "ingles": "eng", "inglés": "eng", "french": "fre", "français": "fre", "francais": "fre",
             "indonesian": "ind", "japanese": "jpn", "日本語": "jpn", "korean": "kor", "한국어": "kor", "spanish": "spa", "castellano": "spa",
             "castilian": "spa", "español": "spa", "german": "ger", "deutsch": "ger", "italian": "ita", "italiano": "ita", "portuguese": "por",
             "brazilian": "por", "russian": "rus", "hindi": "hin", "turkish": "tur", "türkçe": "tur", "polish": "pol", "mandarin": "chi",
             "cantonese": "chi", "国语": "chi", "普通话": "chi", "粤语": "chi", "國語": "chi", "粵語": "chi", "chinese": "chi", "tamil": "tam",
             "telugu": "tel", "thai": "tha", "arabic": "ara", "hebrew": "heb", "dutch": "dut", "swedish": "swe", "danish": "dan", "norwegian": "nor",
             "vietnamese": "vie", "czech": "cze", "hungarian": "hun", "romanian": "rum", "greek": "gre", "ukrainian": "ukr", "persian": "per"}
# The English names of 639-2 codes for alert texts, see lang_name(). ALIAS names the rest.
LANG_NAMES = {"eng": "English", "spa": "Spanish", "fre": "French", "ger": "German", "ita": "Italian", "por": "Portuguese", "jpn": "Japanese",
              "kor": "Korean", "chi": "Chinese", "rus": "Russian", "hin": "Hindi", "tur": "Turkish", "dut": "Dutch", "swe": "Swedish",
              "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "pol": "Polish", "ara": "Arabic", "heb": "Hebrew", "tha": "Thai",
              "ind": "Indonesian", "vie": "Vietnamese", "gre": "Greek", "cze": "Czech", "hun": "Hungarian", "rum": "Romanian", "ukr": "Ukrainian",
              "tam": "Tamil", "tel": "Telugu", "tgl": "Tagalog", "may": "Malay", "bel": "Belarusian", "afr": "Afrikaans", "aze": "Azerbaijani",
              "bos": "Bosnian", "bul": "Bulgarian", "cat": "Catalan", "est": "Estonian", "lit": "Lithuanian", "urd": "Urdu", "und": "untagged"}
NOT_SPEECH = re.compile(r"\w+\s+(score|sub\w*|dub\w*)", re.I)   # audio titles: "UK Dub / Japanese Score"
NOT_SUB_SPEECH = re.compile(r"\w+\s+dub\w*", re.I)                 # subtitle titles keep "English Subtitles"
# Subtitle titles. "English Signs", "Alien Only", "For Foreign Parts Only" and "Titles Only" are forced. "Songs SDH"
# is a full track, so SDH, CC or "full" in the title overrides those words. "English (dub)" and "Dubtitle" transcribe the English dub.
SIGNS_TITLE = re.compile(r"sign|song|foreign|non.?english|alien|parts|titles only|narrat", re.I)
FULL_TITLE = re.compile(r"\bsdh\b|\bcc\b|closed caption|\bfull\b", re.I)
DUB_TITLE = re.compile(r"\bdub", re.I)
FORCED_TITLE = re.compile(r"forced", re.I)
COMMENT_TITLE = re.compile(r"comment|\bcmt\b|\bcomm\b", re.I)   # "Commentary", "cmt", "Director's Comm"
# How sure a track's language is. The tag and the title agree, the title names another language (the title wins), or the tag alone.
AGREE, TITLE_WINS, TAG_ONLY = 1.0, 0.8, 0.6
HEARD = 0.9   # lid.py heard another language than the tag and the title say. The heard language wins.
# A release name that says the audio is English. It lifts an English tag the audio title does not confirm.
ENGLISH_RELEASE = re.compile(r"(?<![a-z])(english|dubbed)(?![a-z])", re.I)
CLASS_NAMES = {"english": "English original", "foreign": "foreign original", "foreign_kids": "foreign kids title"}
SUB_ROLES = ("full", "sdh", "forced", "dub")   # the subtitle roles a policy may list. A commentary subtitle is never a default.
POLICY = None
POLICY_KEYS = ("kids", "audio", "subtitles", "sparse_events", "forced_flag_events", "density_min_minutes", "min_confidence", "forced_clear")
FORCED_CLEAR_KEYS = ("events", "english_only_audio", "reference_ratio")
FORCED_FLAG = "flag-forced"   # the mkvpropedit property of an edit that clears a forced flag
ORIGINAL_FLAG = "flag-original"   # the mkvpropedit property of an edit of the Original language flag, see retag()
# The roles whose Original language flag retag() sets or clears. A commentary or an audio description is no part of the
# content's own speech, so its flag stays as it is.
ORIGINAL_ROLES = {"a": ("main",), "s": ("full", "sdh", "forced", "dub")}


def set_policy(policy):
    """The policy from POLICY_FILE. A missing key is an error, never a silent default."""
    missing = [k for k in POLICY_KEYS if k not in policy]
    missing += [f"forced_clear.{k}" for k in FORCED_CLEAR_KEYS if "forced_clear" in policy and k not in policy["forced_clear"]]
    if missing: raise ValueError("the policy lacks " + ", ".join(missing))
    bad = [x for c in CLASS_NAMES for x in policy["audio"].get(c, [None]) if x not in ("original", "english")]
    bad += [x for x in policy["subtitles"].get("english", [None]) + policy["subtitles"].get("foreign", [None]) if x not in SUB_ROLES]
    if bad: raise ValueError(f"the policy names unknown audio targets or subtitle roles: {bad}")
    global POLICY
    POLICY = policy


def codes(lang):
    """An Arr language name or an ISO 639-2 code -> the set of 639-2 codes a track may carry for it. Empty when unknown."""
    n = ((lang or "").lower().split() or [""])[0]
    if n in UNTAGGED or n == "unknown": return set()
    return next(({c for c in s if len(c) == 3} for s in ALIAS if n in s), {n[:3]})


def lang_name(code):
    """The English name of a 639-2 code: "fre" -> "French". A code in ALIAS takes the name of its set, or its word,
    as "gle" takes "Irish". An unknown code stays as it is."""
    same = next((s for s in ALIAS if code in s), {code or "und"})
    known = [LANG_NAMES[c] for c in sorted(same) if c in LANG_NAMES]
    return known[0] if known else next((w.title() for w in same if len(w) > 3), code)


def lang_names(langs, joint="and"):
    """The names of 639-2 codes, each once, as a person lists them: "English", "English or Japanese"."""
    names = sorted({lang_name(c) for c in langs})
    return f"{', '.join(names[:-1])} {joint} {names[-1]}" if len(names) > 1 else "".join(names)


def kids_title(app, genres, profile_name=None, studio=None):
    """The kids rule applies: a kids genre of the app, a kids quality profile or a kids studio, all from POLICY["kids"]."""
    k = POLICY["kids"]
    return bool(set(k["genres"].get(app, [])) & set(genres or [])) or profile_name in k["profiles"] or studio in k["studios"]


def title_language(title, kind="a"):
    """The one language a track title names, or None. Only an audio title drops a word before Score or Sub."""
    words = (NOT_SPEECH if kind == "a" else NOT_SUB_SPEECH).sub("", title).lower()
    found = {v for k, v in LANGWORDS.items() if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", words)}
    return found.pop() if len(found) == 1 else None


def language(tag, named, heard=None):
    """(language, confidence, why) from the tag, the language the title names, and the language lid.py heard.
    Untagged with no title stays unknown. A heard language that agrees makes the language certain. One that
    disagrees wins at HEARD."""
    if tag in UNTAGGED: lang, conf, why = (named, TITLE_WINS, f"untagged, the title names {named}") if named else (tag, 0.0, "untagged")
    elif not named: lang, conf, why = tag, TAG_ONLY, "tag only"
    elif named in codes(tag): lang, conf, why = tag, AGREE, "tag and title agree"
    else: lang, conf, why = named, TITLE_WINS, f"tagged {tag}, the title names {named}"
    if not heard: return lang, conf, why
    return (lang, AGREE, f"{why}, heard {heard}") if heard == lang or heard in codes(lang) else (heard, HEARD, f"{why}, heard {heard}")


def events_per_minute(j, props):
    """Subtitle events a minute from mkvmerge's statistics tags, or None when mkvmerge did not write them.
    A PGS image subtitle stores about two frames per subtitle, one to show it and one to clear it, so its count is halved."""
    c = (j.get("container") or {}).get("properties") or {}
    minutes = (c.get("duration") or 0) / 6e10; frames = str(props.get("tag_number_of_frames") or "")
    if not (c.get("writing_application") or "").startswith("mkvmerge") or not frames.isdigit() or minutes < POLICY["density_min_minutes"]:
        return None
    return int(frames) / (2 if props.get("codec_id") == "S_HDMV/PGS" else 1) / minutes


def classify(j, heard=None):
    """mkvmerge -J or ffprobe JSON -> the audio and subtitle tracks in file order, each read once.

    Each track has its mkvpropedit selector, lang and conf (see language()), role, and the signals behind the role.
    Audio roles: main, commentary, description. Subtitle roles: full, sdh, forced, dub (a transcript of the English
    dub) and commentary. A subtitle is forced by a "forced" or signs title, by density under POLICY["sparse_events"],
    or by a forced flag on a track under POLICY["forced_flag_events"] or of unknown density. "conflict" is (kind, sentence)
    when the density contradicts a forced flag (dense_forced_flag) or a full or SDH title (sparse_full_title).
    original is the track's Original language flag: 1, 0, or None when the file holds none.
    heard maps an audio track's position ("a2") to the language lid.py heard on it, see language(). It may also map a
    subtitle position to the language retag() gives it from its text.
    """
    out, n = [], {"a": 0, "s": 0}
    for t in j.get("tracks") or j.get("streams") or []:
        p = t.get("properties")
        if p is not None:   # mkvmerge -J. A missing default flag means default, as in the Matroska spec.
            kind = {"audio": "a", "subtitles": "s"}.get(t.get("type"))
            f = dict(lang=p.get("language"), title=p.get("track_name"), ch=p.get("audio_channels"), default=p.get("default_track", True),
                     forced=p.get("forced_track"), comment=p.get("flag_commentary"), described=p.get("flag_visual_impaired"), original=p.get("flag_original"),
                     sdh=p.get("flag_hearing_impaired"), uid=p.get("uid"), codec=t.get("codec"), events=events_per_minute(j, p) if kind == "s" else None)
        else:               # ffprobe
            kind = {"audio": "a", "subtitle": "s"}.get(t.get("codec_type")); d = t.get("disposition") or {}; tg = t.get("tags") or {}
            f = dict(lang=tg.get("language"), title=tg.get("title"), ch=t.get("channels"), default=d.get("default"), forced=d.get("forced"), original=d.get("original"),
                     comment=d.get("comment"), described=d.get("visual_impaired"), sdh=d.get("hearing_impaired"), uid=None, codec=t.get("codec_name"), events=None)
        if not kind: continue
        n[kind] += 1; title = f["title"] or ""; ev = f["events"]
        sel = f"track:={f['uid']}" if f["uid"] else f"track:{kind}{n[kind]}"   # a UID survives any track order
        tag = (f["lang"] or "und").lower()
        h = (heard or {}).get(f"{kind}{n[kind]}")
        lang, conf, why = language(tag, title_language(title, kind), h)
        comment = bool(f["comment"] or COMMENT_TITLE.search(title))
        described = bool(f["described"] or re.search(r"descri", title, re.I) or re.search(r"\bAD\b|DVS", title))
        titled_forced = bool(FORCED_TITLE.search(title))
        signs = titled_forced or bool(SIGNS_TITLE.search(title) and not FULL_TITLE.search(title))
        sparse = ev is not None and ev < POLICY["sparse_events"]
        dense_flag = bool(f["forced"]) and ev is not None and ev >= POLICY["forced_flag_events"] and not titled_forced
        full_title = kind == "s" and bool(FULL_TITLE.search(title) or f["sdh"])
        forced = signs or sparse or (bool(f["forced"]) and not dense_flag)
        sdh = bool(f["sdh"] or re.search(r"\bsdh\b|\bcc\b|hearing", title, re.I))
        dub = kind == "s" and bool(DUB_TITLE.search(title))
        if kind == "a":
            role = "commentary" if comment else "description" if described else "main"
        else:
            role = "commentary" if comment or described else "dub" if dub else "forced" if forced else "sdh" if sdh else "full"
        out.append(dict(kind=kind, sel=sel, pos=f"{kind}{n[kind]}", uid=f["uid"], tag=tag, lang=lang, conf=conf, lang_why=why, title=title,
                        ch=f["ch"] or 0, codec=f["codec"], default=int(bool(f["default"])), role=role, extra=role in ("commentary", "description"),
                        forced=forced, forced_flag=bool(f["forced"]), flagged=bool(f["forced"]) or titled_forced, sdh=sdh, events=ev, heard=h,
                        original=None if f["original"] is None else int(bool(f["original"])),
                        conflict=("dense_forced_flag", f"{kind}{n[kind]} is flagged forced but has {ev:.1f} events a minute") if dense_flag
                        else ("sparse_full_title", f"{kind}{n[kind]} is titled or flagged full or SDH but has {ev:.1f} events a minute")
                        if full_title and sparse else None))
    return out


def prop(e):
    """The Matroska property an edit sets: flag-default, or the property it carries as its fourth item, such as
    FORCED_FLAG or ORIGINAL_FLAG."""
    return e[3] if len(e) > 3 else "flag-default"


def defaults(edits):
    """{selector: new default flag} of the default-flag edits of a plan."""
    return {e[0]: e[1] for e in edits if prop(e) == "flag-default"}


def default_audio(ts, edits=()):
    """The audio track a player starts with: the first one flagged default, else the first one."""
    new = defaults(edits); au = [t for t in ts if t["kind"] == "a"]
    return next((t for t in au if new.get(t["sel"], t["default"])), au[0] if au else None)


def say(plan, code, text=None):
    """A stable reason code for the decision log, and a note for people when text is given. docs/design.md lists the codes."""
    if text: plan["notes"].append(text)
    if code not in plan["reasons"]: plan["reasons"].append(code)


def english_subtitles(su, want, plan):
    """English audio plays: clear every default subtitle except one in a POLICY["subtitles"]["english"] role. When that turns
    an English track off and none of those roles stays on, the sparsest forced-flagged English track in them turns on."""
    keep = set(POLICY["subtitles"]["english"])
    stays = lambda t: t["lang"] == "eng" and t["role"] in keep
    cleared_english = False
    for t in su:
        if t["default"] and not stays(t):
            want[t["sel"]] = 0
            if t["flagged"]: say(plan, "forced_subtitle_cleared", f'cleared default on forced {t["lang"]} subtitle {t["pos"]}')
            cleared_english = cleared_english or t["lang"] == "eng"
    if cleared_english and not any(t["default"] and stays(t) for t in su):
        on = [t for t in su if stays(t)]
        if on:
            f = min(on, key=lambda t: (not t["forced_flag"], t["events"] is None, t["events"] or 0))
            want[f["sel"]] = 1; say(plan, "forced_english_on", f'turned on forced English subtitle {f["pos"]}')


def original_subtitles(su, want, plan):
    """Other audio plays: the best English subtitle becomes the only default, in the role order of POLICY["subtitles"]["foreign"].
    With no English subtitle, only a forced or commentary subtitle loses its default flag. Returns a reason to abstain, or None."""
    order = POLICY["subtitles"]["foreign"]
    en = [t for t in su if t["lang"] == "eng" and t["role"] in order]
    if en:
        s = min(en, key=lambda t: (order.index(t["role"]), t["forced"], t["sdh"]))   # file order breaks a tie
        doubt = [t["conflict"][1] for t in en if t["conflict"] and t["conflict"][0] == "sparse_full_title"]
        if s["role"] == "forced" and doubt:   # the only English tracks look forced, but one says it is full
            return f"{doubt[0]}, and the best English subtitle for other audio would be a forced one"
        want.update({t["sel"]: int(t is s) for t in su})
    else:
        for t in su:
            if t["default"] and (t["flagged"] or t["extra"]):
                want[t["sel"]] = 0
                if t["flagged"]: say(plan, "forced_subtitle_cleared", f'cleared default on forced {t["lang"]} subtitle {t["pos"]}')
        if su: say(plan, "no_english_subtitle", "no English subtitle")


def forced_to_clear(su, main, plan, cls, spoken):
    """English audio plays: (the forced-flagged English subtitles that hold the full dialogue, whose forced flag goes off,
    why an English original keeps the flag or None). A track qualifies at POLICY["forced_clear"]["events"] events a minute
    or more, when classify() calls it full or SDH. So a title that says forced or names signs keeps the flag. With
    english_only_audio, English must be the only main audio language. When the file has other full or SDH subtitles, the
    track must also reach reference_ratio of their median events. A dense forced track of a film with much foreign
    dialogue then keeps its flag.
    An English original also needs TMDB to list English as its only spoken language. spoken is
    TMDB's list, None when TMDB is unknown. When TMDB lists other spoken languages, a dense CC track may carry the
    scenes in those languages. A foreign original that plays its English dub has no such scenes, so the TMDB check
    applies to English originals only."""
    p = POLICY["forced_clear"]
    if p["english_only_audio"] and {t["lang"] for t in main} != {"eng"}: return [], None
    dense = [t for t in su if t["lang"] == "eng" and t["forced_flag"] and t["role"] in ("full", "sdh") and (t["events"] or 0) >= p["events"]]
    if dense and cls == "english" and (spoken is None or set(spoken) != {"eng"}):
        code, why = ("forced_flag_kept_tmdb_unknown", "TMDB is unknown") if spoken is None else \
            ("forced_flag_kept_spoken", f'TMDB lists the spoken languages {", ".join(sorted(spoken))}' if spoken else "TMDB lists no spoken language")
        why = f'{dense[0]["pos"]} keeps its forced flag in an English original, because {why}'
        say(plan, code, why)
        return [], why
    out = []
    for t in dense:
        ref = [x["events"] for x in su if x is not t and x["role"] in ("full", "sdh") and not x["forced_flag"] and x["events"]]
        if ref and t["events"] < p["reference_ratio"] * statistics.median(ref):
            say(plan, "forced_flag_kept_reference", f'{t["pos"]} keeps its forced flag, because {t["events"]:.1f} events a minute is under '
                f'{p["reference_ratio"]:.0%} of the other full subtitles ({statistics.median(ref):.1f})')
            continue
        out.append(t)
        say(plan, "forced_flag_cleared_dense", f'cleared the forced flag on English subtitle {t["pos"]}, {t["events"]:.1f} events a minute')
    return out, None


def audio_target(main, orig, cls):
    """(the main tracks to pick from, the POLICY["audio"] target that matched), or (reason code, reason) to abstain."""
    for target in POLICY["audio"][cls]:
        pick = [t for t in main if t["lang"] in (orig if target == "original" else {"eng"})]
        if pick: return pick, target
        if target == "original" and cls != "english" and any(t["lang"] in UNTAGGED for t in main):
            return "untagged_may_be_original", "the original-language track may be the untagged one"
    return [], None


def decide(j, original, kids=False, release="", heard=None, spoken=None, wrong=None, unmatched=()):
    """The planned default-flag changes for one file. kids is kids_title() for the movie or series, release its release name,
    heard the languages lid.py heard (see classify()), spoken TMDB's spoken languages or None (see forced_to_clear()).
    wrong is retag()'s wrong: a subtitle whose text reads as another language than its tag. When no main audio track
    speaks that language, the decision treats the track as that language, and the track loses its default and forced
    flags. Rather no subtitle than a wrong one. Its tag stays.
    unmatched holds the positions of subtitles whose words do not match the audio (docs/design.md, "Subtitle match").
    Such a track gets the role "unmatched", which no policy lists, so it never becomes a default. It loses its default
    and forced flags.

    Returns {"edits": [[selector, new flag, current flag], ...], "notes", "reasons": stable codes, "wrong_language",
    "undecided": reason or None, "abstain": its code, "dropped": [broken invariants], "invariants": their codes,
    "path": the policy path, "rules" and "edit_rules": what the edits do, "cls", "orig", "tracks": classify()}.
    Empty edits means the file is right, nothing can be done, or the decision abstained.
    """
    if POLICY is None: raise RuntimeError("no policy loaded")
    ts = classify(j, heard); au = [t for t in ts if t["kind"] == "a"]; su = [t for t in ts if t["kind"] == "s"]
    orig = codes(original); main = [t for t in au if t["role"] == "main"]
    cls = "english" if "eng" in orig else "foreign_kids" if kids and orig else "foreign"
    plan = {"edits": [], "notes": [], "reasons": [], "wrong_language": False, "undecided": None, "abstain": None, "dropped": [], "invariants": [],
            "path": CLASS_NAMES[cls], "rules": [], "edit_rules": [], "cls": cls, "orig": sorted(orig), "tracks": ts}
    spoken_here = {lang_key(t["lang"]) for t in main}
    mute = [t for t in su if t["pos"] in (wrong or {}) and lang_key(wrong[t["pos"]]) not in spoken_here]
    for t in mute:
        t.update(lang=wrong[t["pos"]], lang_why=f'{t["lang_why"]}, the text reads {wrong[t["pos"]]}')
    for t in su:
        if t["pos"] in unmatched:
            t.update(role="unmatched", extra=True)   # extra: never a default, see invariants()
            mute += [] if t in mute else [t]
    if any(t["conf"] == TITLE_WINS for t in main): say(plan, "title_language_wins")
    if any(t["conf"] == HEARD for t in main): say(plan, "heard_language")
    elif any(t["heard"] for t in main): say(plan, "heard_confirms")
    got = audio_target(main, orig, cls)
    if isinstance(got[0], str):
        say(plan, *got)
        return dict(plan, undecided=got[1], abstain=got[0])
    pick, target = got
    if not pick:
        plan["wrong_language"] = bool(main) and not any(t["lang"] in UNTAGGED for t in main)
        if plan["wrong_language"]: say(plan, "wrong_language", "no English or original-language audio")
        else: say(plan, "audio_untagged_or_missing", "audio is untagged or missing")
        return plan
    if cls != "english":
        plan["path"] += ", original audio" if target == "original" else ", English dub" if cls == "foreign_kids" else ", no original audio"
    if cls == "foreign_kids" and target == "english":
        say(plan, "kids_dub", "kids or family title, the English dub plays")
    # The app's original is missing from the file, so English plays only when its language is certain.
    if orig and target == "english" and not any(t["lang"] in orig for t in main) and max(t["conf"] for t in pick) < POLICY["min_confidence"]:
        if not ENGLISH_RELEASE.search(release or ""):
            why = f'the app says {original}, no track is {original}, and the English audio is a bare {pick[0]["tag"]} tag'
            say(plan, "original_missing_bare_tag", why)
            return dict(plan, undecided=why, abstain="original_missing_bare_tag")
        say(plan, "release_names_english", "the release name says English, so the bare tag counts")
    cur = default_audio(ts)   # a right default stays, even a late track, and a TrueHD 7.1 track must not beat AC3 5.1
    a = cur if any(cur is t for t in pick) else max(pick, key=lambda t: t["ch"])   # max keeps the first of equal tracks
    if len(pick) > 1: say(plan, "several_main_tracks", f'{len(pick)} main {a["lang"]} tracks, {a["pos"]} ({a["ch"]}ch) plays first')
    # The audio flags change when another track must play first, or when the file has no default audio flag or several.
    # The track that plays then carries the only one.
    flagged = sum(t["default"] for t in au)
    want = {t["sel"]: int(t is a) for t in au} if a is not cur or flagged != 1 else {}
    if a is cur and flagged != 1: say(plan, "audio_flags_normalized", f'{flagged} default audio flags, {a["pos"]} keeps the only one')
    doubt = (english_subtitles if a["lang"] == "eng" else original_subtitles)(su, want, plan)
    for t in mute:
        want[t["sel"]] = 0
        if (t["default"] or t["forced_flag"]) and t["role"] == "unmatched":
            say(plan, "subtitle_audio_mismatch", f'{t["pos"]} loses its default and forced flags: its words do not match the audio')
        elif t["default"] or t["forced_flag"]:
            say(plan, "subtitle_text_muted", f'{t["pos"]} loses its default and forced flags, because its text reads as {t["lang"]}, '
                'and no main audio track speaks it')
    if doubt:
        say(plan, "sparse_full_title", doubt)
        return dict(plan, undecided=doubt, abstain="sparse_full_title")
    # A forced English track with the full dialogue loses its forced flag, so Plex never shows it on its own under English audio.
    clear, kept = forced_to_clear(su, main, plan, cls, spoken) if a["lang"] == "eng" else ([], None)
    edits = [[t["sel"], want[t["sel"]], t["default"]] for t in ts if want.get(t["sel"], t["default"]) != t["default"]]
    edits += [[t["sel"], 0, 1, FORCED_FLAG] for t in clear + [t for t in mute if t["forced_flag"]]]
    if not edits:
        return plan
    final = {t["sel"]: want.get(t["sel"], t["default"]) for t in ts}
    # A forced flag on a dense English subtitle under English audio that forced_to_clear() keeps. In a file with another main
    # audio language the flag is for that audio's viewers (Hindi dual-audio releases), so the default goes off. With English
    # the only language, the flag is the only sign of what the release meant to show, so the decision abstains.
    conflicts = [t["conflict"][1] for t in su if t["conflict"] and t["conflict"][0] == "dense_forced_flag" and t["lang"] == "eng"
                 and t["default"] and not final[t["sel"]] and all(t is not c for c in clear)]
    if a["lang"] == "eng" and conflicts and {t["lang"] for t in main} <= {"eng"} and not any(final[t["sel"]] for t in su if t["lang"] == "eng"):
        why = f"{conflicts[0]}, English is the only audio language, and no sparse forced English track exists" + (f". {kept}" if kept else "")
        say(plan, "dense_forced_flag_english_only", why)
        return dict(plan, undecided=why, abstain="dense_forced_flag_english_only")
    broken = invariants(ts, edits, cls, orig)
    plan["invariants"] = [code for code, _ in broken]
    if broken:
        for code, text in broken: say(plan, code, text)
        return dict(plan, dropped=[text for _, text in broken])
    moved = default_audio(ts, edits) is not cur
    per_edit = [rule_of(next(t for t in ts if t["sel"] == e[0]), e, moved) for e in edits]
    return dict(plan, edits=edits, rules=sorted(set(per_edit)), edit_rules=per_edit)


def rule_of(t, e, moved=True):
    """What one edit does, in words the audit groups by: "audio switched", "English subtitle off", "sdh English subtitle on",
    "forced flag cleared". An audio edit that leaves the same track playing is "audio default flag fixed". Only a subtitle
    turned on names its role, because the role is what the policy ranked."""
    new = e[1]
    if prop(e) == FORCED_FLAG: return "forced flag cleared"
    if t["kind"] == "a": return "audio switched" if moved else "audio default flag fixed"
    if new: return f'{t["role"]} English subtitle on'
    return f'{"English" if t["lang"] == "eng" else "foreign"} subtitle off'


def plan_class(plan):
    """One line that names the policy path and the rules of a plan, for the audit."""
    if plan.get("undecided"): return "undecided"
    if plan.get("dropped"): return "dropped"
    return f'{plan["path"]}: {", ".join(plan["rules"])}' if plan.get("rules") else "no change"


def invariants(ts, edits, cls, orig):
    """The broken invariants of the state a plan leaves, as (reason code, sentence). Checked on every plan with edits."""
    final = {t["sel"]: t["default"] for t in ts}
    final.update(defaults(edits))
    au = [t for t in ts if t["kind"] == "a"]; su = [t for t in ts if t["kind"] == "s"]
    a = default_audio(ts, edits); out = []
    if a is None: return out   # no audio track: nothing plays, and no plan edits such a file
    for target in POLICY["audio"][cls]:   # the first audio target the file has must play: an English original plays English
        langs = orig if target == "original" else {"eng"}
        if any(t["role"] == "main" and t["lang"] in langs for t in au):
            if a["lang"] not in langs:
                out.append(("inv_audio_not_policy_target", f'the {CLASS_NAMES[cls]} would play {a["lang"]} audio, the policy wants {target}'))
            break
    if sum(final[t["sel"]] for t in au) != 1:
        out.append(("inv_audio_default_count", f'{sum(final[t["sel"]] for t in au)} audio tracks would be default'))
    out += [("inv_extra_default", f'{t["role"]} track {t["pos"]} would be default') for t in ts if t["extra"] and final[t["sel"]]]
    english = [t for t in su if t["lang"] == "eng" and not t["extra"]]
    if a["lang"] == "eng":   # under English audio only the roles in POLICY["subtitles"]["english"] may stay on
        out += [("inv_full_english_subtitle_on", f'{t["role"]} English subtitle {t["pos"]} would stay on under English audio')
                for t in english if final[t["sel"]] and t["role"] not in POLICY["subtitles"]["english"]]
    elif any(t["default"] for t in english) and not any(final[t["sel"]] for t in english):
        out.append(("inv_only_english_subtitle_off", f'the plan turns off the only English subtitle under {a["lang"]} audio'))
    return out


# Language tags (docs/design.md, "Language tags"). mkvmerge -J reports a track's legacy ISO 639-2 language and its BCP 47
# language_ietf as the file holds them, and no language_ietf when the file has none. mkvpropedit --set language=<tag>
# writes both elements, and maps the legacy one the way mkvmerge does. A language edit is [selector, new tag, old tag,
# LANG_EDIT]. A LANG_IETF edit follows it when the undo must bring back a BCP 47 tag that --set language cannot, or
# delete the one --set language writes (old tag None).
LANG_EDIT, LANG_IETF = "language", "language-ietf"
BCP47 = re.compile(r"^([a-z]{2,3})(-[a-z]{4})?(-(?:[a-z]{2}|\d{3}))?((?:-[a-z0-9]{1,8})*)$", re.I)
KEEP_TAGS = {"mul", "zxx"}   # multiple languages and no speech are statements, never a missing tag


def language_table(text):
    """`mkvmerge --list-languages` -> ({ISO 639-1, 639-2 or 639-3 code: its 639-2 code}, {639-2 code: the BCP 47
    language mkvmerge writes for it}). A language with no 639-2 code, such as yue or cmn, is in neither map, so its
    tags are never judged."""
    legacy, ietf = {}, {}
    for row in text.splitlines():
        c = [x.strip() for x in row.split("|")]
        if len(c) == 4 and len(c[2]) == 3:
            legacy.update({x: c[2] for x in c[1:] if x})
            ietf[c[2]] = c[3] or c[2]
    return legacy, ietf


def ietf_form(tag):
    """A BCP 47 tag in the form it keeps: the language, a script and a region, in BCP 47 case. A variant or an extension
    goes. A region stays (en-US, pt-BR). A script stays too, because Plex shows it: zh-Hant is 中文（台灣）, zh
    is 中文, and yue-Hant is 粵語. None when tag does not parse."""
    m = BCP47.match(tag or "")
    return m and m[1].lower() + (m[2] or "").title() + (m[3] or "").upper()


def language_tags(j):
    """{track position (a1, s2): (legacy tag, BCP 47 tag or None)} of the audio and subtitle tracks of a mkvmerge -J
    probe, numbered as classify() numbers them."""
    out, n = {}, {"a": 0, "s": 0}
    for t in j.get("tracks") or []:
        kind = {"audio": "a", "subtitles": "s"}.get(t.get("type"))
        if kind and t.get("properties") is not None:   # classify() reads a track without properties as ffprobe's
            n[kind] += 1; p = t.get("properties") or {}
            out[f"{kind}{n[kind]}"] = ((p.get("language") or "und").lower(), p.get("language_ietf"))
    return out


def lang_key(x):
    """One key for the aliases of a language: nob, nno and nor are one, chi holds yue."""
    return min(codes(x) or {x})


def retag(j, heard=None, known=(), table=({}, {}), spoken=(), read=None, original=None, checked=None):
    """The language tag edits of one mkvmerge -J probe (docs/design.md, "Language tags"), as {"edits", "rules", "notes",
    "reasons", "set", "ask", "to_read", "mismatch", "wrong"}. heard maps an audio track's position to the language lid.py
    heard, and read a subtitle track's position to the language text_language() read in its text. known holds the
    item's original languages, the app's and TMDB's, and spoken TMDB's spoken languages, all as 639-2 codes. table is
    language_table().

    A language changes only when two signals agree, and one of them is the heard or the read language. An und subtitle
    takes its read language alone, because its tag makes no claim, unless a title or a BCP 47 tag names a language. The
    track signals are the legacy tag, a BCP 47 tag that names another language, the heard or read language and the
    language the title names. The item's original language is one more signal for a main audio track. A spoken language is one
    only for an und track, because TMDB lists English dialogue as spoken in many foreign films, so it would back
    every lying eng tag. A language wins with two signals or more when no other language has as many. The track
    signals break a tie. Only a main audio track is heard and only a subtitle is read, so no other track changes its
    language. mul and zxx stay. A track that keeps its language gets the form of ietf_form(). A new language keeps the
    BCP 47 tag when that names it or a language inside it: und/yue heard as chi stays yue.
    set maps each main audio or subtitle track whose language changes to its new 639-2 code, for decide(). ask holds the
    main audio tracks not heard yet whose tag a heard language may change: und, two tags that disagree, or a title that
    names another language. to_read holds the subtitle tracks not read yet whose tag a read language may change: an und
    tag, or two signals that disagree. mismatch holds a sentence for each read subtitle whose text reads another
    language than its tag while the tag stays, and wrong maps its position to the read language, for decide(). It counts
    only when text_language() can name the tagged language.

    original is the content's original language, see content_language(). With it, a track of ORIGINAL_ROLES whose
    language AMG is sure of gets the Original language flag: 1 when that language is the original one. A flag of 1 on a
    track in another language goes to 0. A flag already right, and a missing flag that would be 0, stay. The track
    must be tagged after this run, not und, mul or zxx, so an und track this run tags gets its flag in the same run.
    AMG is sure of a main audio track when its heard language names its language
    after this run, or, with nothing heard, its tag and title agree and no other signal names another language. That
    is AGREE, the bar of POLICY["min_confidence"]. A tag alone is not enough, so process.languages() hears such a track
    first, see flag_checks(). AMG is sure of a subtitle when the language read in its text names its language after
    this run. A picture subtitle has no text to read. A track AMG is not sure of keeps its flag, and a note says why.
    checked maps more tracks to the language heard or read for the flag alone, so they change no tag and no other
    finding."""
    legacy_of, ietf_of = table
    key = lang_key
    known, spoken, tags = {key(x) for x in known if x}, {key(x) for x in spoken if x}, language_tags(j)
    readable = {key(x) for x in TEXT_LANGS}
    out = {"edits": [], "rules": [], "notes": [], "reasons": [], "set": {}, "ask": set(), "to_read": set(), "mismatch": [], "wrong": {}}
    for t in classify(j):
        if t["pos"] not in tags:   # an ffprobe probe has no BCP 47 tag
            continue
        (tag, ietf), pos = tags[t["pos"]], t["pos"]
        m = BCP47.match(ietf or "")
        bcp = legacy_of.get(m[1].lower()) if m else None   # the 639-2 language of the BCP 47 tag, None when mkvmerge has none
        split = bool(bcp) and key(bcp) != key(tag)          # the two tags name different languages
        main = t["kind"] == "a" and t["role"] == "main"
        h = (heard or {}).get(pos) if main else (read or {}).get(pos)   # read holds subtitle positions only
        said = f"heard {h}" if main else f"the text reads {h}"
        votes = {}   # language key: [its code, [the signals that name it]]
        for x, why in ((tag, f"tagged {tag}"), (bcp if split else None, f"BCP 47 tag {ietf}"), (h, said),
                       (title_language(t["title"], t["kind"]), f'the title "{t["title"]}"')):
            if x and x not in UNTAGGED:
                votes.setdefault(key(x), [x, []])[1].append(why)
        if main and h is None and tag not in KEEP_TAGS and (tag == "und" or split or len(votes) > 1):
            out["ask"].add(pos)
        if t["kind"] == "s" and h is None and tag not in KEEP_TAGS and (tag == "und" or len(votes) > 1):
            out["to_read"].add(pos)
        item = lambda k: "the item's original language" if k in known else "TMDB's spoken languages" if tag == "und" and not split and k in spoken else None
        score = sorted(((len(s) + bool(main and item(k)), len(s), k) for k, (_, s) in votes.items()), reverse=True)
        win = score[0][2] if score and score[0][0] >= 2 and (len(score) == 1 or score[1][:2] < score[0][:2]) else None
        alone = win is None and t["kind"] == "s" and h and len(votes) == 1   # only the read names a language: an und tag, no title, no BCP 47
        win = key(h) if alone else win
        heard_it = win is not None and said in votes[win][1]
        why = "; ".join(votes[win][1]) + (f", and {item(win)}" if main and item(win) else "") + (", and the und tag names no language" if alone else "") if win else ""
        value = rule = None
        if win and (split or win != key(tag)) and tag not in KEEP_TAGS and heard_it:
            named = key(bcp or m[1].lower()) if m else None   # the language of the BCP 47 tag, a macrolanguage for yue or cmn
            value = ietf_form(ietf) if named == win else ietf_of.get(votes[win][0], votes[win][0])
            rule = "language tag set" if tag == "und" and not split else "language tag corrected"
            if win != key(tag):
                out["set"][pos] = votes[win][0]
        elif ietf and ietf_form(ietf) not in (None, ietf) and not split:
            value, rule, why = ietf_form(ietf), "language tag form", "the form keeps the language, a script and a region"
        elif len(votes) > 1 or split or (tag == "und" and h):   # a tag that looks wrong, or an und track, keeps its language
            out["notes"].append(f"{pos} keeps {tag}{'/' + ietf if ietf else ''}: " + "; ".join(f'{c} ({", ".join(s)})' for c, s in votes.values())
                                + ("" if h else ", no heard language" if t["kind"] == "a" else ", no text read"))
            out["reasons"] += [] if "language_tag_kept" in out["reasons"] else ["language_tag_kept"]
        if value:
            out["edits"].append([t["sel"], value, tag, LANG_EDIT])
            out["rules"].append(rule)
            if ietf != ietf_of.get(tag, tag):   # --set language=<old legacy tag> writes another BCP 47 tag, so the undo sets or deletes it
                out["edits"].append([t["sel"], value, ietf, LANG_IETF])
                out["rules"].append(rule)
            out["notes"].append(f'{pos} {tag}{"/" + ietf if ietf else ""} -> {value}: {why}')
            code = rule.replace(" ", "_")
            out["reasons"] += [] if code in out["reasons"] else [code]
        now = out["set"].get(pos, tag)   # the track's language after this run
        want = int(key(now) == key(original or ""))
        if original and t["role"] in ORIGINAL_ROLES[t["kind"]] and now not in UNTAGGED | KEEP_TAGS \
                and want != (t["original"] or 0) and (want or t["original"]):   # the flag would change
            seen = h or (checked or {}).get(pos)
            if seen:
                sure = key(seen) == key(now)
            else:   # a main audio track whose tag and title agree, see flag_checks()
                sure = main and not split and len(votes) == 1 and len(next(iter(votes.values()))[1]) >= 2
            if sure:
                rule = "original flag set" if want else "original flag cleared"
                out["edits"].append([t["sel"], want, t["original"], ORIGINAL_FLAG])
                out["rules"].append(rule)
                out["notes"].append(f'{pos} {now}: original language flag {t["original"]} -> {want}, the content is {original}')
                out["reasons"] += [] if rule.replace(" ", "_") in out["reasons"] else [rule.replace(" ", "_")]
            else:
                why = (f"heard {seen}" if main else f"the text reads {seen}") + f", not {now}" if seen else \
                    "it was not heard" if main else "its text was not read"
                out["notes"].append(f'{pos} {now}: the original language flag stays, because {why}')
        if t["kind"] == "s" and h and tag not in UNTAGGED | KEEP_TAGS and key(tag) in readable and key(out["set"].get(pos, tag)) != key(h):
            out["mismatch"].append(f"subtitle track {pos[1:]} is tagged {lang_name(tag)}, but its text reads as {lang_name(h)}")
            out["wrong"][pos] = h
            out["reasons"] += [] if "subtitle_text_mismatch" in out["reasons"] else ["subtitle_text_mismatch"]
    return out


def flag_checks(j, original):
    """(the main audio tracks to hear, the subtitles to read) before retag() may change their Original language flag, as
    positions: the tracks of ORIGINAL_ROLES whose flag their tag would change. A main audio track whose tag and title
    agree needs no hearing. original is content_language(), and with none no flag changes."""
    hear, read = set(), set()
    for t in classify(j) if original else ():
        want = int(lang_key(t["tag"]) == lang_key(original))
        if t["role"] not in ORIGINAL_ROLES[t["kind"]] or t["tag"] in UNTAGGED | KEEP_TAGS or want == (t["original"] or 0) \
                or not (want or t["original"]):
            continue
        if t["kind"] == "s":
            read.add(t["pos"])
        elif t["conf"] < AGREE:
            hear.add(t["pos"])
    return hear, read


def content_language(original, expected):
    """The content's original language for the Original language flag, as a 639-2 code: TMDB's, when the app names none
    or the same one. original is the app's name for it, expected content.expected_languages(). None when TMDB is
    unknown, names no 639-2 language, or names another language than the app."""
    tmdb, app = (expected or {}).get("original"), {lang_key(c) for c in codes(original)}
    return tmdb if tmdb and (not app or lang_key(tmdb) in app) else None


def with_tags(plan, tags):
    """plan from decide() with the language edits of retag() added. An undecided or dropped plan gets only the notes."""
    plan = dict(plan, notes=plan["notes"] + tags["notes"], reasons=plan["reasons"] + [r for r in tags["reasons"] if r not in plan["reasons"]])
    if plan.get("undecided") or plan.get("dropped") or not tags["edits"]:
        return plan
    return dict(plan, edits=plan["edits"] + tags["edits"], edit_rules=plan["edit_rules"] + tags["rules"],
                rules=sorted(set(plan["rules"]) | set(tags["rules"])))


def unapplied(after, edits):
    """The edits a mkvmerge -J probe taken after mkvpropedit does not show. A language edit shows as either tag."""
    ts, tags = {t["sel"]: t for t in classify(after)}, language_tags(after)
    def shows(e, t):
        if prop(e) in (LANG_EDIT, LANG_IETF): return e[1] in tags[t["pos"]]
        if prop(e) == ORIGINAL_FLAG: return (t["original"] or 0) == e[1]
        return (t["default"] if prop(e) == "flag-default" else int(t["forced_flag"])) == e[1]
    return [e for e in edits if e[0] not in ts or not shows(e, ts[e[0]])]


# Subtitle text language (docs/design.md, "Subtitle text"). A count of common words names the language of a subtitle
# text, with no model and no dependency. Each list holds frequent dialogue words of one language. A word in one list
# only is a telling word. A word in several lists counts only for the cover of each.
TEXT_STEP = 300      # letters between two counts. A text with fewer letters is short and gets no answer.
TEXT_STOP = 3000     # letters after which the count stops, about 600 words
TEXT_TELL = 25       # telling words the top language needs for an answer
TEXT_COVER = 0.2     # the share of all words the top language's whole list must hold. A language with no list holds less.
TEXT_SHARE = 0.9     # the share of the telling words, or of the letters for a script, the top language must hold
TEXT_MIXED = 0.75    # a text whose top script or language holds less than this share is mixed at once. More stays open until TEXT_STOP.
STOPWORDS = {k: frozenset(v.split()) for k, v in {
    "eng": "the you to and it of that what this have your my for not be do are don know just can with all get but there they she him "
           "her his like right well if go out up how about want now come think why who did will would been were had could should going one "
           "got let from when because here me no we is was on in so i a at or an care even ten face",
    "spa": "que de no a la el y es en lo un por me se una te los con para mi qué eso esto está esta pero yo muy bien sí si ya su tu aquí aqui "
           "ahora hay nada estoy tengo puedo quiero sé cuando donde dónde algo todo más mas cómo como también tambien usted ella él eres soy "
           "gracias señor senor porque del al le les nos ha va son las solo este ser ti ve dar pelo cosa van",
    "por": "que de não nao o a e é um uma eu você voce isso com se me do da em na no os as mas ele ela muito bem também tambem então entao "
           "obrigado obrigada sim está esta estou tenho aqui agora nós meu minha seu sua onde quando porque nada tudo foi vai vou já só ao "
           "pelo pela das lá senhor para por coisa ser fazer pode como te este todo à sei nos mais dar algo",
    "fre": "je de pas le la vous est que tu à et les un ne il ce qui on une en c j pour des mais qu me elle a moi bien du non plus au avec "
           "lui sais suis oui ai fait tout rien ça va si nous sont êtes était cette comme où quoi alors ici peut veux dans sur mon ton son "
           "très te ta ma as dit mal",
    "ger": "ich du die der und nicht das ist sie es zu ein wir was mir ja in den mit sich auf mich dich hier eine so wie aber sein hat habe "
           "haben kann noch nur schon wenn wo warum gut jetzt nein danke bitte bin bist war von dem für auch alles doch mal weiß um an er "
           "ihn ihm uns euch dir als am will des",
    "ita": "che non di il è la un per mi ma a in si ti lo le cosa sono come questo bene sei ho hai ha qui io tu lui lei noi voi mio tuo "
           "suo sua con del della da gli una perché fatto solo ci anche sta niente adesso molto grazie signore allora dove quando tutto chi "
           "più così essere fare vai va o e c",
    "rum": "și si să sa că ca nu este e în in pe ce mai eu tu el ea am ai o un cu se te mă ma ne asta acum aici dar da bine știu stiu "
           "vreau poți poti avem sunt ești esti fost face ceva nimic doar tot unde cum când cand pentru despre din lui fi va voi dacă daca "
           "care la de a are iar îmi imi meu noi au al hai or",
    "dut": "ik je het de een niet dat is en wat van in ze hij we zijn er op te maar met me die voor dit hebt heb heeft kan wel nog naar "
           "ben bent was jij mijn hier zo goed weet waar waarom nee ja ook alles niks niets dan als om moet wil zal hoe wie nu toch even "
           "gaan komen had sta nou ons iets",
    "afr": "ek jy hy sy ons julle hulle nie vir sal sê baie dis hom hoekom asseblief dankie kry gesê mense iemand niemand altyd miskien "
           "regtig môre vandag lyk moenie die het was en van in op te dat wat dit is maar ook om met moet wil gaan kan weet hier daar waar "
           "waarom hoe wie nee ja goed alles niks een voor nog dan my jou haar iets nou",
    "swe": "jag du det att inte är en och som på har vi vad med för han hon den mig dig sig ska kan så om var här nu men till av ett bara "
           "nej ja hur vill kommer vet inget ingenting allt där också eller min din honom henne detta gör måste oss er då blev mer alltid "
           "dem sa igen kanske säga blir bli nog",
    "dan": "jeg du det at ikke er en og i på har vi hvad med for han hun den mig dig sig skal kan så om var her nu men til af et bare "
           "nej ja hvordan vil kommer ved noget intet alt der også eller min din ham hende dette sige gør være meget havde blev os hvorfor "
           "da lige nogen sådan mere altid som dem kun selv hvis hvor godt nok skulle ingen fordi tror undskyld "
           "måske tale sagde gik lad bliver blive fik sidste lidt hvornår skete jer øjeblik hjælpe igen tak have op mit",
    "nor": "jeg du det at ikke er en og i på har vi hva med for han hun den meg deg seg skal kan så om var her nå men til av et bare "
           "nei ja hvordan vil kommer vet noe ingenting alt der også eller min din ham henne dette si gjør være mye hadde ble oss hvorfor "
           "da sånn noen mer alltid som dem kun selv hvis hvor godt nok skulle ingen fordi tror sa kanskje "
           "snakke igjen blir bli gikk fikk litt siste skjedde unnskyld dere øyeblikk hjelpe ett",
    "pol": "nie to się w i na jest że z co jak ale tak do ja ty mi go mnie już tu czy o jestem jesteś być był była może wiem mam masz "
           "nic tylko teraz dobrze dlaczego gdzie kiedy proszę dziękuję dla po od za przez ten ta te bardzo wszystko jeszcze tego my wy "
           "on ona ci cię mu ze a żeby jeśli będzie sobie jej nas tam tutaj coś ktoś nigdy zawsze też więc bo no chcę "
           "możesz musisz musimy trzeba wiesz chodź jego mój moja twój naprawdę dobra tej tym nawet gdy pan pani",
    "cze": "je se to na že v a s ne jsem jsi jsme jste jsou co jak ale tak už ještě taky tady teď proč kde když můžu mám máš vím víš chci "
           "nevím protože jo jen být byl bylo něco nic nikdo všechno dobře děkuju prosím pane tě mě ti mi tebe mně jeho její můj tvůj tohle "
           "takže tam pak opravdu musím musíme chceš jestli dneska zítra já ho ty z dnes",
    "slo": "je sa to na že v a s nie som si sme ste sú čo ako ale tak už ešte tiež tu teraz prečo kde keď môžem mám máš viem vieš chcem "
           "neviem pretože áno len byť bol bolo niečo nič nikto všetko dobre ďakujem prosím pane ťa ma ti mi teba mne jeho jej môj tvoj toto "
           "takže tam potom naozaj musím musíme chceš ak dnes zajtra ho z ich",
    "tur": "bir bu ne ve için de da mı mi mu mü ben sen o çok var yok değil ama ile gibi daha şey evet hayır tamam şimdi burada nasıl "
           "neden kim bunu beni seni onu bana sana ona benim senin sadece lazım zaman iyi bak hadi gel git en her kadar şu biliyorum "
           "ya ki bey hep ise yani işte peki niye biz siz onlar hiç bile sonra önce zaten belki hemen lütfen diye çünkü artık olsun oldu "
           "olur misin musun mısın değilim istiyorum gerek tabii orada nerede bunlar şunu bunun onun bizim sizin seninle benimle efendim",
    "rus": "и в не на я что он с это как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из "
           "ему теперь когда ну если уже или быть был него до вас опять вам ведь там потом себя ничего ей может они тут где есть надо "
           "ней для мы тебя их чем была сам без тоже себе под будет тогда кто этот хорошо почему здесь тебе",
    "ukr": "і в не на я що він з це як а то все вона так його але та ти до у же ви за б по тільки її мені було ось від мене ще ні о "
           "із йому тепер коли ну якщо вже або бути був нього вас знову вам там потім себе нічого їй може вони тут де є треба ній для "
           "ми тебе їх чим була сам без теж собі під буде тоді хто цей добре чому",
}.items()}
TELLING = {k: s - frozenset().union(*(o for j, o in STOPWORDS.items() if j != k)) for k, s in STOPWORDS.items()}
# A script that names one language. Latin and Cyrillic go to the word lists. Han with kana is Japanese. Arabic script
# with Persian letters is Persian. Another script names no language.
SCRIPTS = {"GREEK": "gre", "HEBREW": "heb", "ARABIC": "ara", "THAI": "tha", "HANGUL": "kor", "HIRAGANA": "jpn", "KATAKANA": "jpn",
           "CJK": "chi", "DEVANAGARI": "hin", "TAMIL": "tam", "TELUGU": "tel", "GEORGIAN": "geo", "ARMENIAN": "arm"}
PERSIAN = frozenset("پچژگکی")   # letters Persian writes and Arabic does not
SOUTH_CYRILLIC = frozenset("јљњћђџѓќѕ")   # Serbian and Macedonian letters. Russian and Ukrainian never write them.
TEXT_LANGS = frozenset(STOPWORDS) | frozenset(SCRIPTS.values()) | {"per"}   # every language text_language() can name
# Common Han characters of Chinese dialogue, simplified and traditional. Chinese text holds many of them. The bytes of
# another codepage decoded as Chinese give rare characters, which hold almost none, see text_judge().
HAN_COMMON = frozenset("的一是不了在人有我他这這个個们們中来來上大为為和国國地到以说說时時要就出会會可也你对對生能而子那得于着著下自之年"
                       "过過发發后後作里裡用道行所然家种種事成方多经經么麼去法学學如都同现現当當没沒动動面起看定天分还還进進好小部其些主样樣"
                       "理心她本前开開但因只从從想实實吗嗎呢吧啊什知怎谁誰哪走让讓给給再太很真跟把被别別请請谢謝嗯喂哦呀快回做告诉訴听聽等"
                       "叫找问問已爱愛死东東西妈媽爸先姐点點头頭儿兒孩係唔佢嘅咗冇啲喇咁嘢")
HAN_SHARE = 0.2   # the share of the Han characters that HAN_COMMON must hold for text to read as Chinese
# Greek writes ς only at the end of a word. Hebrew bytes decoded as cp1253 give it inside words, for the letter ע. So
# text with ς inside more than this share of its words is no Greek, see text_judge(). A few words of real Greek that
# lost the space after their ς weigh much at the first counts, so the count reads on to TEXT_STOP.
GREEK_INNER_SIGMA = 0.01
TEXT_NOISE = re.compile(r"<[^>]*>|\{[^}]*\}|\\[Nnh]")   # SubRip tags, ASS override blocks and ASS line breaks
TEXT_FOLD = str.maketrans("şţё", "șțе")   # Romanian cedillas to commas below, and Russian ё to е, as the lists spell them
WORD = re.compile(r"[^\W\d_]+")


def script_of(c):
    """The script of one letter, as the first word of its Unicode name: LATIN, CYRILLIC, CJK."""
    if c < "\u0250":
        return "LATIN"
    return unicodedata.name(c, "OTHER").split(" ")[0].split("-")[0]


def text_judge(words, scripts, letters):
    """(language or None, confidence, why, final) from the counts text_language() keeps. final is False when the top
    script holds between TEXT_MIXED and TEXT_SHARE of the letters, as a few names in Latin letters early in Greek
    dialogue do. It is also False when the top language has too few telling words yet, or holds between TEXT_MIXED and
    TEXT_SHARE of them. More text may decide then."""
    if scripts.get("HIRAGANA", 0) + scripts.get("KATAKANA", 0) > 0.1 * scripts.get("CJK", 0):
        scripts = dict(scripts, KATAKANA=scripts.get("KATAKANA", 0) + scripts.get("CJK", 0), CJK=0)   # kanji in Japanese
    top = max(scripts, key=scripts.get)
    n = sum(v for k, v in scripts.items() if SCRIPTS.get(k, k) == SCRIPTS.get(top, top))
    if n < TEXT_SHARE * letters:
        return None, round(n / letters, 2), f"mixed scripts, {n / letters:.0%} {top.lower()}", n < TEXT_MIXED * letters
    if top not in ("LATIN", "CYRILLIC"):
        lang = SCRIPTS.get(top)
        if lang == "ara" and sum(k * sum(c in PERSIAN for c in w) for w, k in words.items()) > 0.05 * n:
            lang = "per"
        if lang == "chi" and sum(k * sum(c in HAN_COMMON for c in w) for w, k in words.items()) < HAN_SHARE * n:
            return None, round(n / letters, 2), f"{n / letters:.0%} of the letters are cjk, but few are common Chinese characters", True
        if lang == "gre" and sum(k for w, k in words.items() if "ς" in w[:-1]) > GREEK_INNER_SIGMA * sum(words.values()):   # more text may decide
            return None, round(n / letters, 2), f"{n / letters:.0%} of the letters are greek, but ς sits inside words", False
        return lang, round(n / letters, 2), f"{n / letters:.0%} of the letters are {top.lower()}" + ("" if lang else ", which names no one language"), True
    tell = {k: sum(words.get(w, 0) for w in s) for k, s in TELLING.items()}
    lang = max(tell, key=tell.get)
    if lang in ("rus", "ukr") and any(c in SOUTH_CYRILLIC for w in words for c in w):   # Serbian shares most Russian stopwords
        return None, 0.0, f"Serbian or Macedonian letters rule out {lang}", True
    if tell[lang] < TEXT_TELL:
        return None, 0.0, f"{tell[lang]} telling words", False
    share, cover = tell[lang] / sum(tell.values()), sum(words.get(w, 0) for w in STOPWORDS[lang]) / sum(words.values())
    if cover < TEXT_COVER:
        return None, round(share, 2), f"no list fits, {lang} words are {cover:.0%} of the text", True
    if share < TEXT_SHARE:
        second = sorted(tell.values())[-2]
        return None, round(share, 2), f"mixed, {lang} holds {share:.0%} of the telling words, the next {second / sum(tell.values()):.0%}", share < TEXT_MIXED
    return lang, round(share, 2), f"{share:.0%} of the telling words are {lang}, its words are {cover:.0%} of the text", True


def text_language(cues):
    """(ISO 639-2 language or None, confidence, why) of the text of subtitle cues. cues is an iterable of cue texts, read
    only as far as the answer needs. Tags and ASS override blocks drop out. The letters decide the script first. A
    script of one language names it, and Latin or Cyrillic text goes to the word lists. There the language with the
    most telling words wins when it holds TEXT_SHARE of all telling words, has TEXT_TELL of them, and its whole list
    holds TEXT_COVER of the text. The count runs every TEXT_STEP letters. It stops at the first verdict or at TEXT_STOP
    letters. A text under TEXT_STEP letters is short. Mixed text and a language with no list get None. A share under
    TEXT_MIXED is mixed at once, and a share just under TEXT_SHARE reads on, because a few early words weigh most."""
    words, scripts, letters, check = {}, {}, 0, TEXT_STEP
    for text in cues:
        text = TEXT_NOISE.sub(" ", text).replace("İ", "i").lower().translate(TEXT_FOLD)
        for c in text:
            if c.isalpha():
                s = script_of(c); scripts[s] = scripts.get(s, 0) + 1; letters += 1
        for w in WORD.findall(text):
            words[w] = words.get(w, 0) + 1
        if letters >= check:
            got = text_judge(words, scripts, letters)
            if got[3] or letters >= TEXT_STOP:
                return got[:3]
            check = min(letters + TEXT_STEP, TEXT_STOP)
    if letters < TEXT_STEP:
        return None, 0.0, f"short, {letters} letters"
    return text_judge(words, scripts, letters)[:3]


def sidecar_language(named, read, audio):
    """What the text of a sidecar subtitle changes before a conversion muxes it, or None (docs/design.md, "Subtitle
    text"). named is the ISO 639-2 language its file name gives, None for none. read is text_language() of its text,
    and audio holds the languages of the file's main audio tracks. The text overrules the name only when
    text_language() is sure, and only when it can name the language of the name too. Returns (the language to mux the
    sidecar with, True when its name may still set the forced flag, a note for the log). A player shows a forced track
    in the audio's language by itself, so the forced flag of the name stays only when the text is in an audio language."""
    lang = read[0]
    if not (lang and named) or lang_key(named) not in {lang_key(x) for x in TEXT_LANGS} or lang_key(named) == lang_key(lang):
        return None
    return lang, lang_key(lang) in {lang_key(a) for a in audio}, f"named {named}, but the text reads as {lang}: {read[2]}"


# Legacy codepages (docs/design.md, "Subtitle text"), by the name mkvmerge and Python share, with the languages each one
# writes. A sidecar that is not UTF-8 or UTF-16 is in one of them, and so is the text an old muxer read as cp1252, see
# recode(). The codepages of Chinese, Japanese and Korean come first. Their decode of other text fails on most bytes,
# and a decode that passes gives rare Han characters, see HAN_COMMON. A one-byte codepage decodes most bytes, and
# Greek, Hebrew and Arabic letters name their language by their script alone. Serbian is in Latin letters or in Cyrillic.
CODEPAGES = {"cp950": "chi", "gbk": "chi", "cp932": "jpn", "cp949": "kor",
             "cp1252": "eng spa por fre ger ita dut afr swe dan nor fin ice cat glg baq ind may wel gle alb",
             "cp1250": "pol cze slo rum hun hrv slv bos srp", "cp1251": "rus ukr bul bel srp mac", "cp1253": "gre", "cp1254": "tur aze",
             "cp1255": "heb", "cp1256": "ara per urd", "cp1257": "lit lav est"}
CP1252_HOLES = (0x81, 0x8D, 0x8F, 0x90, 0x9D)   # the bytes cp1252 leaves undefined
# cp1252 with each undefined byte as the control character of its value, as Windows reads it. Python's codec raises on them.
CP1252_TEXT = "".join(chr(b) if b in CP1252_HOLES else bytes([b]).decode("cp1252") for b in range(256))
CP1252_MAP = codecs.charmap_build(CP1252_TEXT)
CP1252_RUNS = re.compile("[" + re.escape(CP1252_TEXT) + "]+")   # the runs of a text that cp1252 writes
REPAIR_CUT = 0.02   # the share of a track's cues a read-back may find cut short, see subtitles.garbled_tracks()
# The share of the letters a read-back must change to show garbled text, see recode(). In its right codepage, text
# changes only in a few borrowed words, such as an Italian "perchè" in Romanian dialogue.
GARBLE_SHARE = 0.01
# The letters outside ASCII of the languages a one-byte codepage writes in Latin letters, and the script of the others.
# A read-back keeps a character as it was when it would give a letter outside them, see read_back().
ALPHABETS = {"pol": "ąćęłńóśźż", "cze": "áčďéěíňóřšťúůýž", "slo": "áäčďéíĺľňóôŕšťúýž", "rum": "ăâîșțşţ", "hun": "áéíóöőúüű",
             "hrv": "čćđšž", "bos": "čćđšž", "srp": "čćđšž", "slv": "čšž", "tur": "çğıöşüâîûİ", "aze": "çəğıöşüİ",
             "lit": "ąčęėįšųūž", "lav": "āčēģīķļņšūž", "est": "äöõüšž"}
SCRIPT_LANGS = {"CYRILLIC": "rus ukr bul bel srp mac", "GREEK": "gre", "HEBREW": "heb", "ARABIC": "ara per urd"}
SYMBOLS = ("So", "Sc")   # the symbols a read-back always gives, as № and € of cp1251. Marks such as ˇ and ˛ stay kept.


def writes(cp):
    """The language keys of the languages the codepage cp of CODEPAGES writes. UTF-8 writes them all, as None."""
    return None if cp == "utf-8" else {lang_key(x) for x in CODEPAGES[cp].split()}


def reads_as(texts, cp):
    """The language text_language() reads in texts, when the codepage cp writes it, else None."""
    lang = text_language(texts)[0]
    return lang if lang and (cp == "utf-8" or lang_key(lang) in writes(cp)) else None


def plausible(text):
    """Whether a decode gives the letters of one script, and in Latin letters mostly ASCII ones. Cyrillic bytes
    decoded as cp1250 give Latin letters too, but hardly any ASCII ones."""
    letters = [c for c in text if c.isalpha()]
    scripts = {}
    for c in letters:
        scripts[script_of(c)] = scripts.get(script_of(c), 0) + 1
    top = max(scripts, key=scripts.get, default=None)
    return bool(top) and scripts[top] >= TEXT_SHARE * len(letters) and (top != "LATIN" or sum(c.isascii() for c in letters) >= len(letters) / 2)


def letter_of(c, lang):
    """Whether c is a letter that lang writes: ASCII, one of its ALPHABETS letters or a letter of its script. A
    language with neither entry takes every letter."""
    key = lang_key(lang)
    alphabet = next((v for k, v in ALPHABETS.items() if lang_key(k) == key), None)
    script = next((s for s, ls in SCRIPT_LANGS.items() if key in {lang_key(x) for x in ls.split()}), None)
    if alphabet is None and script is None:
        return c.isalpha()
    return c.isascii() or c in (alphabet or "") or c.lower() in (alphabet or "") or (script is not None and script_of(c) == script)


def read_back(text, cp, lang=None):
    """(text as the codepage cp reads its cp1252 bytes, True when it ends cut inside a character), see recode().
    A character that cp1252 does not write stays as it is, as a right "♪" a later tool added. Raises UnicodeError
    when the bytes of a run between such characters do not decode as cp. With lang, a one-byte codepage keeps a
    character as it was when its read-back is neither a letter lang writes, see letter_of(), nor a symbol of SYMBOLS.
    A right "Señor" in Romanian text then stays, where cp1250 would give "Seńor", and Russian "№" reads back. See kept()
    for the letters that stay too."""
    out, cut, runs = [], False, list(CP1252_RUNS.finditer(text))
    pos = 0
    for k, m in enumerate(runs):
        out.append(text[pos:m.start()])   # the characters cp1252 does not write
        pos = m.end()
        raw = codecs.charmap_encode(m[0], "strict", CP1252_MAP)[0]
        dec = codecs.getincrementaldecoder(cp)()
        back = dec.decode(raw)   # a character cut at the end waits in the decoder
        try:
            dec.decode(b"", final=True)
        except UnicodeDecodeError:
            if k < len(runs) - 1 or pos < len(text):   # only the end of a cue can be cut
                raise
            back, cut = back + "\N{REPLACEMENT CHARACTER}", True
        if lang and cp.startswith("cp125") and len(back) == len(m[0]):
            back = "".join(g if g != b and kept(m[0], i, b, lang) else b for i, (g, b) in enumerate(zip(m[0], back)))
        out.append(back)
    out.append(text[pos:])
    return "".join(out), cut


def kept(run, i, b, lang):
    """Whether a one-byte read-back keeps run[i] as it was, where the codepage gives b, see read_back(). It keeps a
    character whose read-back is neither a letter lang writes, see letter_of(), nor a symbol of SYMBOLS. It keeps a
    Latin letter right after a digit, as the "º" of "40.5ºC", which cp1250 gives as "ş", unless a lowercase letter
    follows, as in a word joined to a number: "9876ºase" reads back as "9876şase". It keeps a letter of another
    script in a word that holds ASCII letters too, as the "é" of "Café", which cp1251 gives as "й". Garbled Cyrillic,
    Greek, Hebrew and Arabic words hold no ASCII letters, because those codepages write their letters above 0x7F."""
    if not (letter_of(b, lang) or unicodedata.category(b) in SYMBOLS):
        return True
    if not b.isalpha():
        return False
    if script_of(b) == "LATIN":
        # "¹".isdigit() holds, and cp1250 reads it as "ą"
        return i > 0 and run[i - 1] in "0123456789" and not (i + 1 < len(run) and run[i + 1].islower())
    lo, hi = i, i + 1
    while lo and run[lo - 1].isalpha():
        lo -= 1
    while hi < len(run) and run[hi].isalpha():
        hi += 1
    return any(c.isascii() for c in run[lo:hi])


def recode(texts):
    """[(codepage, the language the texts read as, the texts read back, the count of texts cut short)] of each codepage
    of UTF-8 and CODEPAGES that reads cue texts back, in that order (docs/design.md, "Garbled subtitle repair"). An old
    muxer that took a sidecar's bytes for cp1252 wrote each byte as the character cp1252 gives it. So the cp1252 bytes
    of the text are the sidecar's bytes, and read_back() reads them as the codepage they were in. A codepage counts
    when every text decodes, the read-back changes GARBLE_SHARE of the letters at least, and the texts read as a
    language it writes. UTF-8 counts with no language too, as when the muxer cut most cues. Text in another codepage
    does not decode as UTF-8, because that needs pairs such as "Ã©" that real text does not hold. The language is
    None then. In a codepage of more than one byte, each character outside ASCII that cp1252 writes counts as changed. The muxer
    cut a cue at a byte cp1252 leaves undefined. A cue cut inside a character, as UTF-8 always cuts it, gets U+FFFD
    there and counts as cut. A cue cut between two characters only ends early, and the count misses it. In a one-byte
    codepage that is every cut, such as at Ť and ť of cp1250."""
    if not any(not t.isascii() for t in texts):
        return []
    letters, out = sum(c.isalpha() for t in texts for c in t), []
    for cp in ("utf-8", *CODEPAGES):
        if cp == "cp1252":
            continue
        try:
            got = [read_back(t, cp) for t in texts]
        except UnicodeError:
            continue
        back = [t for t, _ in got]
        changed = sum(sum(a != b for a, b in zip(t, u)) if len(t) == len(u) else sum(not c.isascii() and c in CP1252_TEXT for c in t)
                      for t, u in zip(texts, back))
        if changed >= GARBLE_SHARE * letters and ((lang := reads_as(back, cp)) or cp == "utf-8"):
            out.append((cp, lang, back, sum(c for _, c in got)))
    return out


def garbled(texts, tag):
    """The verdict on the cue texts of a SubRip track tagged tag, or None when they are not garbled (docs/design.md,
    "Garbled subtitle repair"). The texts are garbled when they hold enough letters and a codepage of recode() reads
    them back. A UTF-8 reading wins over the others: Hebrew letters read back from UTF-8 cut at every cue give a few
    letters that read as Hebrew by their script alone. Else the reading in the tag's language wins, then one in any
    language, then the first one. The verdict is
    {"repair", "codepage", "lang", "read", "cut", "cues", "why"}. repair is True under the repair rule: the reading
    reads as the tag's language. read is the language the texts read as."""
    texts = list(texts)
    read = text_language(texts)
    got = recode(texts) if not read[2].startswith("short") else []
    if not got:
        return None
    got = [g for g in got if g[0] == "utf-8"] or got   # text that decodes as UTF-8 is UTF-8, even when the muxer cut most of it
    cp, lang, _, cut = min(got, key=lambda g: (lang_key(g[1]) != lang_key(tag), g[1] is None))
    why = None if lang and lang_key(lang) == lang_key(tag) else f"the track is tagged {tag}"
    return {"repair": not why, "codepage": cp, "lang": lang, "read": read[0], "cut": cut, "cues": len(texts),
            "why": f"read back as {cp}, the text reads as {lang or 'no language'}" + (f", but {why}" if why else "")}


def repair_fault(texts, tag):
    """Why the repaired cue texts of a track tagged tag may not go in, or None. They must read as the tag's language,
    and garbled() must find no reading in them, as it finds in text garbled twice."""
    read = text_language(texts)[0]
    if lang_key(read) != lang_key(tag):
        return f"the repaired text reads as {read or 'no language'}, not as {tag}"
    if g := garbled(texts, tag):
        return f"the repaired text is still garbled: {g['why']}"
    return None


def duration(j):
    """Container duration in seconds, or 0 when the probe has none."""
    if "container" in j: return ((j["container"].get("properties") or {}).get("duration") or 0) / 1e9   # mkvmerge may give null
    return float((j.get("format") or {}).get("duration") or 0)


def checks(j, size, runtime, shorter_only=False):
    """Alerts for one probed file: [(kind, facts)]. size is in bytes, runtime is the listed runtime in minutes (0 = unknown).

    duration: the container duration disagrees with the size. The BPS statistics tags give the expected length, but
    only when mkvmerge wrote the file and every audio and video track carries one. ffmpeg copies stale tags from its
    source. Otherwise a bitrate ceiling decides: 50 Mbit/s up to 1080p (the Blu-ray mux rate is 48), 150 above.
    runtime:  the file runs more than 40 percent off the listed runtime. Listed runtimes under 10 minutes are skipped.
    shorter_only (Sonarr) alerts only under 0.6 x the listed runtime, because double episodes and premieres run long.
    A broken duration suppresses the runtime alert, because the header is wrong, not the content.
    """
    dur, out = duration(j), []
    if not dur: return out
    items = j.get("tracks") or j.get("streams") or []
    av = [(t.get("properties") or {}) for t in items if t.get("type") in ("video", "audio")]
    app = ((j.get("container") or {}).get("properties") or {}).get("writing_application") or ""
    bps = sum(int(p["tag_bps"]) for p in av) if app.startswith("mkvmerge") and av and all(str(p.get("tag_bps", "")).isdigit() for p in av) else 0
    mmss = f"{int(dur // 3600)}:{int(dur % 3600 // 60):02d}:{int(dur % 60):02d}" if dur >= 3600 else f"{int(dur // 60)}:{int(dur % 60):02d}"
    if bps:
        est = size * 8 / bps
        if abs(dur - est) > 0.4 * est:
            out.append(("duration", {"why": f"The file says it runs {mmss}, but at its own bitrate {size / 1e9:.1f} GB lasts about {est / 60:.0f} "
                                            "minutes."}))
    else:
        dims = [(t.get("properties") or {}).get("pixel_dimensions") or f'{t.get("width", 0)}x{t.get("height", 0)}'
                for t in items if t.get("type") == "video" or t.get("codec_type") == "video"]
        height = max([int(d.split("x")[1]) for d in dims if "x" in d] or [0])
        if size * 8 / dur > (150e6 if height > 1080 else 50e6):
            out.append(("duration", {"why": f"The file says it runs {mmss}, but that would mean {size * 8 / dur / 1e6:.0f} Mbit/s for "
                                            f"{size / 1e9:.1f} GB, far more than a real video."}))
    off = dur / 60 < 0.6 * runtime if shorter_only else abs(dur / 60 - runtime) > 0.4 * runtime
    if runtime >= 10 and not out and off:
        out.append(("runtime", {"runs": mmss, "listed": runtime}))   # report.FINDINGS words it
    return out


SAMPLE_AT = (0.10, 0.50, 0.85)   # where the three audio samples start, as a share of the duration
SAMPLE_SECS = 20
# Read-only tests on real files set these values:
# - quiet real audio peaks far above SILENT_DB. Digital silence decodes to -91 dB.
# - a healthy sample decodes nearly all of the audio its own output stream line promises.
# - a healthy file may log an error in all three samples, from the seek (a cut first packet, mp3 "Header missing")
#   or the demuxer ("keyframes not correctly marked"). So an error alone never counts, only an error with lost audio.
# - a container duration longer than the content leaves the late sample empty with no error. Only a real cut logs
#   "File ended prematurely" (mkv) or "partial file" (mp4).
SILENT_DB = -80.0                # a sample whose loudest point is at or below this is digital silence
LOSS = 0.95                      # a sample that decodes less than this share of its expected audio lost some
SHORT_AUDIO = 0.9                # tagged audio that ends before this share of the tagged video is truncated
HARMLESS = ("keyframes not correctly marked",)
CUT = ("File ended prematurely", "partial file")
LAYOUTS = {"mono": 1, "stereo": 2, "downmix": 2, "quad": 4, "hexagonal": 6, "octagonal": 8, "cube": 8, "hexadecagonal": 16}


def channels(layout):
    """ffmpeg's channel layout name -> channel count, or None when unknown."""
    m = re.match(r"(\d+)\.(\d+)", layout) or re.match(r"(\d+) channels", layout)
    if m: return sum(int(g) for g in m.groups())
    return LAYOUTS.get(layout.split("(")[0])


def parse_sample(stderr, rc=0):
    """One ffmpeg volumedetect run with -loglevel level+info -> a dict.

    n: decoded samples, all channels. max: peak dB or None. errors: error lines that matter. ran: ffmpeg exited 0 with
    no fatal line, so the sample means something. cut: the file ended early. rate and ch: from ffmpeg's own output
    stream line, never from the container's declared values. ffmpeg may print a first volumedetect block for a probe
    graph, so the last value of each field counts.
    """
    last = lambda k: (re.findall(rf"{k}: (-?[0-9.]+|-?inf)", stderr) or [None])[-1]
    mx = last("max_volume")
    out = stderr.split("Output #0", 1)[1] if "Output #0" in stderr else ""
    m = re.search(r"Audio: [^,]+, (\d+) Hz, ([^,]+),", out)
    return {"n": int(last("n_samples") or 0), "max": float(mx) if mx not in (None, "-inf", "inf") else (-91.0 if mx else None),
            "errors": sum("[error]" in line and not any(h in line for h in HARMLESS) for line in stderr.splitlines()),
            "ran": rc == 0 and "[fatal]" not in stderr, "cut": any(c in stderr for c in CUT),
            "rate": int(m.group(1)) if m else None, "ch": channels(m.group(2).strip()) if m else None}


def tag_seconds(value):
    h, m, sec = (str(value).split(":") + ["0", "0"])[:3] if value and str(value).count(":") == 2 else (0, 0, 0)
    return int(h) * 3600 + int(m) * 60 + float(sec)


def audio_verdict(j, a_index, samples, runtime=0):
    """Broken audio for the track that will play. Returns (certain reason or None, [uncertain reasons]).

    samples are parse_sample() results, each with "window", its length in seconds. Any sample that did not run makes
    the whole verdict uncertain: an unreadable file or a stream ffmpeg does not have says nothing about the audio.
    Certain: digital silence in all three samples, lost audio with decode errors in all three, or a late sample that
    hits the end of a cut file. A cut needs the listed runtime (minutes) as a cross-check: the late sample must start
    before 95 percent of it, so a duration header longer than the content never reads as a cut. The caller decides
    "no audio track" (a_index None) after ffprobe agrees, and checks that ffprobe and mkvmerge count the same tracks.
    Uncertain: any of those in one or two samples, an empty late sample with no cut, a sample that lost audio without
    an error, or tagged audio that ends well before the tagged video. The tags count only when mkvmerge wrote the file.
    """
    if a_index is None: return "the file has no audio track", []
    n = len(samples)
    if not all(s.get("ran", True) for s in samples):
        return None, [f"the audio check could not run at {sum(not s.get('ran', True) for s in samples)} of {n} places"]
    ratio = [s["n"] / (s["rate"] * s["ch"] * s["window"]) if s.get("rate") and s.get("ch") and s.get("window") else 1.0 for s in samples]
    silent = [s["n"] > 0 and s["max"] is not None and s["max"] <= SILENT_DB for s in samples]
    broken = [s["errors"] > 0 and r < LOSS for s, r in zip(samples, ratio)]
    if all(silent): return f"the audio is silent at all {n} places checked", []
    if all(broken): return f"the audio fails to play at all {n} places checked", []
    # A cut counts only when the late sample decoded nothing. A cut inside the late window may be a lost tail
    # under a long duration header, which reads as lost audio, so it only alerts.
    late = samples[-1]
    if late.get("cut") and late["n"] == 0 and runtime > 0 and late.get("at", 0) < 0.95 * runtime * 60:
        return "the file is cut off before the last place checked", []
    doubts = ["the file may end early, near the last place checked"] if late.get("cut") else []
    if any(silent): doubts.append(f"the audio is silent at {sum(silent)} of {n} places checked")
    if any(broken): doubts.append(f"the audio fails to play at {sum(broken)} of {n} places checked")
    if samples[-1]["n"] == 0: doubts.append("no audio plays at the last place checked, maybe because the file says it runs longer than it does")
    if any(s["n"] == 0 for s in samples[:-1]): doubts.append("no audio plays at an earlier place checked")
    lost = [f"{r:.0%}" for s, r, b in zip(samples, ratio, broken) if s["n"] and r < LOSS and not b]
    if lost: doubts.append(f"part of the audio is missing, and only {', '.join(lost)} of it plays where it was checked")
    items = j.get("tracks") or []
    app = ((j.get("container") or {}).get("properties") or {}).get("writing_application") or ""
    au = [t for t in items if t.get("type") == "audio"]; vi = [t for t in items if t.get("type") == "video"]
    if app.startswith("mkvmerge") and vi and a_index < len(au):
        a, v = (tag_seconds((x.get("properties") or {}).get("tag_duration")) for x in (au[a_index], vi[0]))
        if a and v and a < SHORT_AUDIO * v:
            doubts.append(f"the audio stops at {clock(a)}, but the video runs to {clock(v)}")
    return None, doubts


# The checks after the three samples (docs/design.md, "Broken audio"). The hook reads, these functions decide.
FULL_UNDER = 600       # seconds: a shorter file gets its playing audio track decoded in full
FULL_SHARE = 0.9       # a full decode under this share of the track's own span lost audio
HELD_SHARE = 0.9       # a constant-rate track that holds less audio than this share of the video lost some
EMPTY = 0.05           # a sample that decodes less than this share of its window decoded nothing
HOLE_MIN = 30.0        # seconds a hole in the audio packets needs before a sample goes into it. Lost audio leaves holes of
                       # minutes. A film intermission may leave a hole of a few seconds, so a short hole stays a doubt.
HOLE_GAP = 1.0         # seconds between two audio packets that count as lost audio in held. Frame durations rounded to the
                       # millisecond leave gaps under 1 ms, and a 44.1 kHz AC-3 track at 34 ms a frame sums to 97.6 percent.
VIDEO_GAP = 10.0       # seconds between two video packets, or of one video packet, that make the timeline unfit for a certain
                       # verdict: a stray packet hours late, a PTS wrap, a joined capture, or a broken MP4 stts entry that
                       # gives the last frame hours.
CBR = ("ac3", "eac3", "dts")   # ffprobe codec names of constant bit rate codecs. DTS-HD and DTS Express vary, see held().
CBR_TAGS = ("AC-3", "E-AC-3", "DTS")   # the same codecs by mkvmerge's name
DIALOGUE = 4.0         # events a minute that make a text subtitle track a dialogue track. A forced track holds signs only.
BITMAP_SUBS = ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "S_HDMV/PGS", "S_VOBSUB", "S_DVBSUB")   # never a dialogue track:
                       # a picture event is two packets, so a forced PGS track with 2 signs a minute reads as 4
# The first bytes of a container ffmpeg or mkvmerge reads: (offset, bytes). A first byte 0x47 counts as MPEG-TS, and a
# fifth as M2TS, so a random file may match by chance and then gets no verdict.
SIGNATURES = ((0, b"\x1a\x45\xdf\xa3"), (4, b"ftyp"), (4, b"moov"), (4, b"mdat"), (4, b"free"), (4, b"wide"), (4, b"skip"),
              (0, b"RIFF"), (0, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"), (0, b"\x47"), (4, b"\x47"), (0, b"\x00\x00\x01\xba"),
              (0, b"\x00\x00\x01\xb3"), (0, b"FLV"), (0, b"OggS"), (0, b".RMF"))


def clock(seconds):
    """Seconds as a player shows them, cut to the second: 4:17, 1:02:05."""
    h, m, s = int(seconds // 3600), int(seconds % 3600 // 60), int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def media_signature(b):
    """Whether b, the first bytes of a file, starts like a container in SIGNATURES."""
    return any(b[at:at + len(sig)] == sig for at, sig in SIGNATURES)


def lost(s):
    """Whether a sample that ran decoded nothing, or less than LOSS of the audio its output stream line promises."""
    full = (s.get("rate") or 0) * (s.get("ch") or 0) * (s.get("window") or 0)
    return bool(s.get("ran", True) and (s["n"] == 0 or (full and s["n"] < LOSS * full)))


def empty(s):
    """Whether a sample with its window decoded nothing: under EMPTY of the audio its output stream line promises."""
    full = (s.get("rate") or 0) * (s.get("ch") or 0) * (s.get("window") or 0)
    return s.get("ran", True) and (s["n"] < EMPTY * full if full else s["n"] == 0)


def playing(j, a_index):
    """The audio track a_index of mkvmerge -J or ffprobe JSON, {} when it has none."""
    au = [t for t in j.get("tracks") or [] if t.get("type") == "audio"] or [s for s in j.get("streams") or [] if s.get("codec_type") == "audio"]
    return au[a_index] if a_index < len(au) else {}


def cbr(j, a_index):
    """Whether the audio track a_index uses a constant bit rate codec, by mkvmerge's or ffprobe's codec name."""
    t = playing(j, a_index)
    return t.get("codec") in CBR_TAGS or t.get("codec_name") in CBR


def mkvmerge_tags(j, t):
    """The properties of track t when mkvmerge wrote its statistics tags for this file, else None. ffmpeg copies stale tags."""
    app = ((j.get("container") or {}).get("properties") or {}).get("writing_application") or ""
    p = t.get("properties") or {}
    return p if app.startswith("mkvmerge") and p.get("tag_duration") and p.get("tag__statistics_writing_app", app) == app else None


def tag_held(j, a_index):
    """Seconds of audio the statistics tags say a constant-rate track holds: its frames times its frame duration. None
    unless mkvmerge wrote the tags for this file."""
    t = playing(j, a_index)
    p = mkvmerge_tags(j, t) or {}
    if t.get("codec") in CBR_TAGS and str(p.get("tag_number_of_frames", "")).isdigit() and p.get("default_duration"):
        return int(p["tag_number_of_frames"]) * p["default_duration"] / 1e9
    return None


def subtitle_kinds(j):
    """(subtitle tracks, text subtitle tracks, the latest DURATION tag of a subtitle track or None) of mkvmerge -J or
    ffprobe JSON. A DURATION tag runs from the first event to the end of the last, so a track ends no earlier."""
    subs = [t for t in j.get("tracks") or [] if t.get("type") == "subtitles"] or [s for s in j.get("streams") or [] if s.get("codec_type") == "subtitle"]
    codec = lambda t: (t.get("properties") or {}).get("codec_id") or t.get("codec_name")
    tags = [tag_seconds(p["tag_duration"]) for p in (mkvmerge_tags(j, t) for t in subs) if p]
    return len(subs), sum(codec(t) not in BITMAP_SUBS for t in subs), max(tags, default=None)


def stops_early(samples, pk):
    """Certain when every sample decoded nothing, the audio packets end before the first sample starts and the video
    packets run past the start of the late one with no gap or packet over VIDEO_GAP. pk is the packet read of the file, its times
    from the file start."""
    if len(samples) == 3 and pk.get("audio") is not None and pk.get("video") and pk["video_gap"] <= VIDEO_GAP \
            and all(s.get("ran", True) and s["n"] == 0 for s in samples) and pk["audio"] < samples[0]["at"] and pk["video"] > samples[-1]["at"]:
        return f"the audio stops at {clock(pk['audio'])}, but the video runs to {clock(pk['video'])}"
    return None


def held(pk):
    """The doubt when a constant-rate audio track holds less than HELD_SHARE of the video, else None. held counts the
    audio packets' time with no bit rate, so a rate change inside the track counts right. A video with a gap over
    VIDEO_GAP has no length to compare with."""
    profile = pk.get("profile") or ""
    if pk.get("codec") not in CBR or "HD" in profile or "Express" in profile or pk.get("held") is None or not pk.get("video") \
            or pk["video_gap"] > VIDEO_GAP or pk["held"] >= HELD_SHARE * pk["video"]:
        return None
    return f"the audio track holds only {clock(pk['held'])} of sound for {clock(pk['video'])} of video"


def full_verdict(full):
    """(certain, doubt) of the full decode of a short file. It compares the seconds decoded with the track's own span,
    from its first frame to its end, so audio that starts late by design loses nothing. Under FULL_SHARE with decode
    errors is certain, without errors a doubt."""
    if not full.get("ran") or not full.get("end") or full["decoded"] >= FULL_SHARE * full["end"]:
        return None, None
    text = f"only {clock(full['decoded'])} of the {clock(full['end'])} audio track plays"
    return (f"{text}, with {full['errors']} errors", None) if full["errors"] else (None, text)


def lone_late_silence(samples):
    """Whether only the late one of three samples is digital silence, the shape of silent end credits."""
    return len(samples) == 3 and [s["n"] > 0 and s["max"] is not None and s["max"] <= SILENT_DB for s in samples] == [False, False, True]


def credits_silence(samples, subs, span):
    """The end of the last subtitle event when a lone silent late sample starts after it, else None. That silence is
    the end credits. subs lists [end, events, text] per subtitle track, span
    is the seconds the samples cover. Every track counts for the end. At least one text track must hold DIALOGUE events a
    minute, so a file with no subtitles, with forced ones only or with picture ones only keeps its doubt."""
    if not lone_late_silence(samples) or not subs or not span or not any(text and n >= DIALOGUE * span / 60 for _, n, text in subs):
        return None
    last = max(e for e, _, _ in subs)
    return last if last <= samples[-1]["at"] else None


# The video check (docs/design.md, "Corrupt video"). It has three stages, the Matroska Segment size, a zero probe and
# three decoded windows. Each runs only when the earlier ones found nothing certain.
VIDEO_AT = (0.10, 0.50, 0.85)   # where the windows start, as a share of the duration, the audio SAMPLE_AT
VIDEO_AGAIN = (0.30, 0.70, 0.95)   # the windows of the second check before a re-grab, apart from VIDEO_AT
VIDEO_SECS = 5                   # seconds decoded per window
VIDEO_TIMEOUT = 60               # seconds a window may run. An AVI with no index seeks from the start, which is slow.
VIDEO_MAX_READ = 512 << 20       # bytes a window may read. A clean window reads far less. A seek with no index may read gigabytes.
ZERO_READS = 256                 # 64 KiB reads of the zero probe, 16 MiB per file
ZERO_EDGE = 0.01                 # the zero probe skips the first and last 1 percent, where headers and padding live
ZERO_RUN = 256 << 10             # a zero page is a hit only in a zero run this long. An encoder pads easy frames with shorter
                                 # zero runs, and real damage leaves longer ones.
SHORT_CERTAIN = 64 << 10         # missing bytes that make a short Matroska file certain. One lost Usenet article is hundreds of KiB.
                                 # A file a few bytes short after a failed mkvpropedit run may keep every cluster intact, a doubt.
LATE = 30                        # seconds to the first frame that mean no video at the window. An MPEG-TS seek may land
                                 # several seconds past the position.
GAP = 1.0                        # seconds between two output frames that mean lost video. Clean windows have shorter steps.
# The null muxer's timestamp complaints, ffmpeg's repeat counter, and a malformed SEI message the h264 decoder skips. A file
# may log "SEI type 0 size 5 truncated at 4" at every keyframe and lose no frame.
# The mpeg4 decoder logs the low_delay line once after a seek into packed B-frames. Clean avi files log it in a window,
# and never in a full decode.
NOISE = ("[null @", "Last message repeated", "SEI type", "low_delay flag set incorrectly")
NO_DECODER = "no decoder found"   # ffmpeg has no decoder for the video track, such as encrypted video (encv)
# ffmpeg's evidence of an encrypted track: the CENC FourCC of a sample entry, and the key id of a Matroska ContentEncryption
ENCRYPTED = re.compile(r"\bencv\b|\benc_key_id\b")
DEMUX = ("[matroska", "[mov,", "[avi @", "[mpegts @", "[asf @", "[mpeg @", "[flv @")   # container errors count anywhere
FRAME = re.compile(r"Parsed_showinfo.*\bn:\s*\d+.*\bpts_time:\s*(-?[0-9.]+)")


def parse_window(stderr, rc=0):
    """One window's ffmpeg stderr (-loglevel level+info, showinfo filter) -> a dict.

    errors: container error lines anywhere, and decoder error lines and corrupt-frame warnings after the first output
    frame. A decoder error before the first frame belongs to the seek. Clean windows log such lines, and a UHD remux
    seek logs "PPS changed between slices". A container error there counts, because a seek with no
    index reads through the damage on its way. gap: the longest step between output frames. late: the first
    frame came over LATE seconds after the window start, so the seek found no video there. empty: no frame at all.
    noisy: an empty window logged errors. cut: the file ended early. ran: ffmpeg exited 0, or the file was cut.
    nodecoder: ffmpeg found no decoder for the video track, so the window could not run. encrypted: ffmpeg names an
    encrypted track, see ENCRYPTED.
    """
    pts, errors, noisy = [], 0, False
    for line in stderr.splitlines():
        m = FRAME.search(line)
        if m:
            pts.append(float(m.group(1)))
        elif any(k in line for k in ("[error]", "[fatal]", "corrupt decoded frame")) and not any(n in line for n in NOISE + CUT):
            errors += bool(pts) or (line.startswith(DEMUX) and "[error]" in line)
            noisy = noisy or "[error]" in line
    cut = any(c in stderr for c in CUT)
    return {"frames": len(pts), "errors": errors, "gap": round(max((b - a for a, b in zip(pts, pts[1:])), default=0), 2),
            "late": bool(pts) and pts[0] > LATE, "empty": not pts, "noisy": not pts and noisy, "cut": cut,
            "ran": (rc == 0 and "[fatal]" not in stderr) or cut, "nodecoder": NO_DECODER in stderr,
            "encrypted": bool(ENCRYPTED.search(stderr))}


def bad_window(w):
    """Why one window that ran is bad, or None. An empty window with no error is a doubt, see video_verdict(). An empty
    window with errors is bad in a cut file too. A file that lost bytes early fails every later packet."""
    if w["late"] or w["noisy"]:
        return "no video"
    if w["errors"]:
        return f"{w['errors']} playback error" + ("s" if w["errors"] > 1 else "")
    if w["gap"] > GAP:
        return f"{w['gap']:.0f} s of video missing"
    return None


def stopped_doubt(w):
    """The doubt of a window that the read cap or the time cap stopped."""
    return f"the video check at {clock(w['at'])} {w['stopped']}, so the file may have no usable index"


def read_capped(w):
    """True for a window that the read cap stopped with no error on the way. A seek with no index for the video reads
    the file from its start. A file that indexes the video once can decode in full with no error."""
    return (w.get("stopped") or "").startswith("read over") and not w["errors"] and not w["noisy"]


def video_verdict(zero_hits, windows):
    """(certain reason or None, [doubts]) from the zero probe's offsets and the windows.

    Certain: zero runs of ZERO_RUN or more at 2 or more offsets, 2 or more windows with no decoder for an encrypted
    video track, or 2 or more bad windows. With no evidence of encryption such windows did not run, a doubt: a codec
    ffmpeg does not know may be a healthy file. A doubt: such a run at one offset, one bad window, a window stopped by the time limit, a window
    that did not run, or an empty window. An empty window with no error is a cut file, which the audio check judges in
    Matroska, or    a window past the end of the video. A window that the read cap stopped with no error is only logged.
    """
    if len(zero_hits) >= 2:
        return f"the file has blank gaps at {len(zero_hits)} of {ZERO_READS} places checked, so the download is incomplete", []
    nodecoder = sum(bool(w.get("nodecoder") and w.get("encrypted")) for w in windows)
    if nodecoder >= 2:
        return f"the video is encrypted and cannot play at {nodecoder} of {len(windows)} places checked", []
    doubts = [f"the file has a blank gap at {zero_hits[0]:.0%} of its length"] if zero_hits else []
    bads = []
    for w in windows:
        why = bad_window(w) if w["ran"] else None
        if why:
            bads.append(f"{why} at {clock(w['at'])}")
        elif read_capped(w):
            continue
        elif w.get("stopped"):
            doubts.append(stopped_doubt(w))
        elif not w["ran"]:
            doubts.append(f"the video at {clock(w['at'])} could not be checked")
        elif w["empty"]:
            doubts.append(f"no video at {clock(w['at'])}" + (", where the file is cut off" if w["cut"] else ""))
    if len(bads) >= 2:
        return f"the video is broken at {len(bads)} of {len(windows)} places checked: " + ", ".join(bads[:-1]) + " and " + bads[-1], doubts
    return None, doubts + bads


# The header check (docs/design.md, "Header repair"). A lossless mkvmerge remux rewrites the Matroska Segment duration, the
# Cues and the Segment size from the real streams. These functions parse Matroska bytes the script reads.
EBML_ID, SEGMENT, SEEKHEAD, SEEK, SEEK_ID, SEEK_POS = 0x1A45DFA3, 0x18538067, 0x114D9B74, 0x4DBB, 0x53AB, 0x53AC
CLUSTER, CUES, CUEPOINT, CUETIME, CUETRACKPOS, CUETRACK, TIMESTAMP = 0x1F43B675, 0x1C53BB6B, 0xBB, 0xB3, 0xB7, 0xF7, 0xE7
SIMPLEBLOCK, BLOCKGROUP, BLOCK, BLOCKDURATION, VOID, CRC = 0xA3, 0xA0, 0xA1, 0x9B, 0xEC, 0xBF
# The level-1 elements of a Segment: Info, Tracks, Chapters, Attachments and Tags beside the named ones.
LEVEL1 = {SEEKHEAD, 0x1549A966, 0x1654AE6B, 0x1043A770, CLUSTER, CUES, 0x1941A469, 0x1254C367, VOID, CRC}
# The children of a Cluster: SilentTracks, Position, PrevSize and EncryptedBlock beside the named ones.
IN_CLUSTER = {TIMESTAMP, 0x5854, 0xA7, 0xAB, SIMPLEBLOCK, BLOCKGROUP, 0xAF, VOID, CRC}
HEADER_OFF, HEADER_SHARE = 60, 0.02   # the header duration is wrong when it is off the stream end by more than the larger of these
AV_APART = 60                         # seconds the video and the audio may end apart. More is a sign of real damage. Clean
                                      # films may end tens of seconds apart.
REPAIR_END = 1.0                      # seconds a repaired header may differ from the stream end
FRAME_SLACK = 3                       # video frames a remux may come short of the video end over the default frame duration
REMOVE_SHARE = 0.10                   # a SubRip track with this share of its lines starting after the real end is timed for another
                                      # cut, so it is removed, not trimmed. A track that is wrong in general
                                      # is better gone.
REMOVE_MIN = 2                        # and at least this many late lines, so one stray line in a 6-line forced track is a trim
# A cut file ends early while its good subtitles run to the full length. Its late tracks end near the listed runtime.
# A mistimed or runaway track ends far past it.
CUT_END = 0.95     # the video and the audio of a complete file end at this share of the listed runtime or later
REMOVE_END = 1.2   # a removed track ends past this share of the listed runtime
TEXT_SUBS = ("S_TEXT/UTF8",)          # the subtitle codecs a trim can cut: SubRip
SRT_TIME = re.compile(r"^\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})(.*)$")


def element(b, p):
    """(id, data start, size) of the EBML element at p of b, or None when b holds no whole id and size there. size is
    None for the unknown size of a live stream."""
    if p >= len(b) or not b[p]:
        return None
    n = 9 - b[p].bit_length(); q = p + n
    if n > 4 or q >= len(b) or not b[q]:
        return None
    m = 9 - b[q].bit_length()
    if q + m > len(b):
        return None
    s = int.from_bytes(b[q:q + m], "big") & ((1 << 7 * m) - 1)
    return int.from_bytes(b[p:q], "big"), q + m, None if s == (1 << 7 * m) - 1 else s


def children(b, start, end):
    """The (id, data start, size) of each element from start to end. Stops at the first one that does not fit."""
    p, end = start, min(end, len(b))
    while p < end:
        e = element(b, p)
        if not e or e[2] is None or e[1] + e[2] > end:
            return
        yield e
        p = e[1] + e[2]


def segment_start(b):
    """(the file offset of the Segment data, the Segment size or None) from the first bytes of a file, None when it is
    not Matroska."""
    e = element(b, 0)
    s = e and e[0] == EBML_ID and e[2] is not None and element(b, e[1] + e[2])
    return (s[1], s[2]) if s and s[0] == SEGMENT else None


def seek_entries(b, start, end, into):
    """Add {element id: [positions relative to the Segment data]} from the SeekHead data b[start:end] to into."""
    for i, d, s in children(b, start, end):
        if i == SEEK:
            got = {ci: b[cd:cd + cs] for ci, cd, cs in children(b, d, d + s)}
            if SEEK_ID in got and SEEK_POS in got:
                into.setdefault(int.from_bytes(got[SEEK_ID], "big"), []).append(int.from_bytes(got[SEEK_POS], "big"))
    return into


def front_seeks(b, start):
    """The seek_entries() of every SeekHead among the level-1 elements of b from start up to the first Cluster."""
    seek, p = {}, start
    while (e := element(b, p)) and e[0] in LEVEL1 and e[0] != CLUSTER and e[2] is not None:
        if e[0] == SEEKHEAD:
            seek_entries(b, e[1], e[1] + e[2], seek)
        p = e[1] + e[2]
    return seek


def last_cues(b):
    """{track number: its last CueTime in ticks} of the data of a whole Cues element."""
    out = {}
    for i, d, s in children(b, 0, len(b)):
        if i == CUEPOINT:
            got = list(children(b, d, d + s))
            t = next((int.from_bytes(b[cd:cd + cs], "big") for ci, cd, cs in got if ci == CUETIME), None)
            for ci, cd, cs in got:
                if ci == CUETRACKPOS and t is not None:
                    for n in (int.from_bytes(b[td:td + ts], "big") for ti, td, ts in children(b, cd, cd + cs) if ti == CUETRACK):
                        out[n] = max(out.get(n, t), t)
    return out


def cue_blocks(b, tracks, cap):
    """{track number: [(Cluster position, position inside the Cluster data, CueDuration in ticks or None)]} of the first
    cap cue entries of each track in tracks, from the data of a whole Cues element. A film's Cues hold tens of
    thousands of cue points, and a walk over each one in Python is slow. So this search finds the CueTrackPositions of
    the wanted tracks by their bytes: a one-byte CueTrack, then the CueClusterPosition and the CueRelativePosition, in
    the order mkvmerge and ffmpeg write them, and a CueDuration after them when there is one. mkvmerge and ffmpeg write
    one for each subtitle block. An entry in another shape is left out."""
    # ponytail: a byte search. A muxer that writes the elements in another order or size gives no entries, so no answer.
    out = {n: [] for n in tracks if 0 < n < 128}
    if not out:
        return {}
    for m in re.finditer(rb"\xf7\x81([" + re.escape(bytes(sorted(out))) + rb"])\xf1([\x81-\x88])", b):
        n, p = m[1][0], m.end() + (m[2][0] & 0x7F)
        r = len(out[n]) < cap and b[p:p + 1] == b"\xf0" and element(b, p)
        if r and r[2] and r[1] + r[2] <= len(b):
            d = b[r[1] + r[2]:r[1] + r[2] + 1] == b"\xb2" and element(b, r[1] + r[2])
            out[n].append((int.from_bytes(b[m.end():p], "big"), int.from_bytes(b[r[1]:r[1] + r[2]], "big"),
                           int.from_bytes(b[d[1]:d[1] + d[2]], "big") if d and d[2] and d[1] + d[2] <= len(b) else None))
            if all(len(v) >= cap for v in out.values()):
                break
    return {n: v for n, v in out.items() if v}


def block_frame(b, number):
    """The frame of the SimpleBlock or BlockGroup at the start of b, or None when b holds no whole block of track number
    there. A laced block holds several frames, and it gets None too."""
    e = element(b, 0)
    if e and e[2] is not None and e[1] + e[2] <= len(b) and e[0] == BLOCKGROUP:
        e = next((c for c in children(b, e[1], e[1] + e[2]) if c[0] == BLOCK), None)
    if not e or e[2] is None or e[1] + e[2] > len(b) or e[0] not in (SIMPLEBLOCK, BLOCK) or not e[2]:
        return None
    d = e[1]; k = 9 - b[d].bit_length()   # the track number is an EBML variable-size integer
    if not 0 < k <= 4 or e[2] < k + 3 or int.from_bytes(b[d:d + k], "big") & ((1 << 7 * k) - 1) != number or b[d + k + 2] & 0x06:
        return None
    return b[d + k + 3:d + e[2]]


def block_head(b, number):
    """(the frame's first bytes, (the timestamp relative to its Cluster, None)) of the SimpleBlock, or of the Block that
    opens a BlockGroup, at the start of b, as block_frame() and block_times() give them. b may end inside the block,
    since the first bytes of a picture subtitle say whether it shows or clears. (None, None) when b holds no block of
    track number there, or a laced one."""
    e = element(b, 0)
    if e and e[0] == BLOCKGROUP:
        e = element(b, e[1])
    if not e or e[2] is None or e[0] not in (SIMPLEBLOCK, BLOCK) or e[1] >= len(b):
        return None, None
    d = e[1]; k = 9 - b[d].bit_length()   # the track number is an EBML variable-size integer
    if not 0 < k <= 4 or e[2] < k + 3 or len(b) < d + k + 3 or int.from_bytes(b[d:d + k], "big") & ((1 << 7 * k) - 1) != number \
            or b[d + k + 2] & 0x06:
        return None, None
    return b[d + k + 3:min(len(b), d + e[2])], (int.from_bytes(b[d + k:d + k + 2], "big", signed=True), None)


def block_times(b):
    """(the timestamp relative to its Cluster, the BlockDuration or None) of the SimpleBlock or BlockGroup at the start
    of b, or None when b holds no whole block there. Both are in ticks of the file's timestamp scale."""
    e = element(b, 0)
    if not e or e[2] is None or e[1] + e[2] > len(b) or e[0] not in (SIMPLEBLOCK, BLOCKGROUP):
        return None
    dur = None
    if e[0] == BLOCKGROUP:
        kids = list(children(b, e[1], e[1] + e[2]))
        dur = next((int.from_bytes(b[d:d + s], "big") for i, d, s in kids if i == BLOCKDURATION), None)
        e = next((c for c in kids if c[0] == BLOCK), None)
    if not e or not e[2]:
        return None
    k = 9 - b[e[1]].bit_length()   # the track number is an EBML variable-size integer
    if not 0 < k <= 4 or e[2] < k + 3:
        return None
    return int.from_bytes(b[e[1] + k:e[1] + k + 2], "big", signed=True), dur


def cluster_blocks(b, start, end, durations, ends):
    """Add the end tick of each block of one Cluster, b[start:end], to ends {track number: tick}. A block ends at its
    timestamp plus its BlockDuration, else its frames times the track's default duration. False when the bytes are no
    Cluster: a child that is not a Cluster child, or a block before the Cluster Timestamp. A Cluster cut by the end of
    b may end in a part of a child."""
    ts, p = None, start
    for i, d, s in children(b, start, end):
        if i not in IN_CLUSTER or (i in (SIMPLEBLOCK, BLOCKGROUP) and ts is None):
            return False
        if i == TIMESTAMP:
            ts = int.from_bytes(b[d:d + s], "big")
        blk = [(d, s, None)] if i == SIMPLEBLOCK else []
        if i == BLOCKGROUP:
            got = {ci: (cd, cs) for ci, cd, cs in children(b, d, d + s)}
            if BLOCK in got:
                dur = got.get(BLOCKDURATION)
                blk = [(*got[BLOCK], int.from_bytes(b[dur[0]:dur[0] + dur[1]], "big") if dur else None)]
        for bd, bs, dur in blk:
            n = 9 - b[bd].bit_length() if b[bd] else 9
            if n > 8 or bs < n + 4:
                return False
            track = int.from_bytes(b[bd:bd + n], "big") & ((1 << 7 * n) - 1)
            rel, laced = int.from_bytes(b[bd + n:bd + n + 2], "big", signed=True), b[bd + n + 2] & 0x06
            frames = b[bd + n + 3] + 1 if laced else 1
            end_tick = ts + rel + (dur if dur is not None else frames * durations.get(track, 0))
            ends[track] = max(ends.get(track, end_tick), end_tick)
        p = d + s
    if ts is None:
        return False
    if end <= len(b):
        return p == end
    e = element(b, p)   # cut by the end of b: what is left is one child cut short
    return len(b) - p < 12 or bool(e and e[0] in IN_CLUSTER and e[2] and e[1] + e[2] > len(b))


TRACKS, TRACKENTRY, TRACKTYPE, CONTENTENCODINGS, CONTENTENCODING, CONTENTENCRYPTION = 0x1654AE6B, 0xAE, 0x83, 0x6D80, 0x6240, 0x5035
MP4_BOXES = {b"moov": 0, b"trak": 0, b"mdia": 0, b"minf": 0, b"stbl": 0, b"stsd": 8, b"encv": 78, b"sinf": 0, b"schi": 0}   # skip to the children


def mkv_encrypted_video(b):
    """True when the data of a Matroska Tracks element holds a video TrackEntry with a ContentEncryption element."""
    for i, d, s in children(b, 0, len(b)):
        got = {ci: (cd, cs) for ci, cd, cs in children(b, d, d + s)} if i == TRACKENTRY else {}
        if TRACKTYPE in got and int.from_bytes(b[got[TRACKTYPE][0]:sum(got[TRACKTYPE])], "big") == 1 and CONTENTENCODINGS in got:
            cd, cs = got[CONTENTENCODINGS]
            if any(ei == CONTENTENCODING and any(x == CONTENTENCRYPTION for x, _, _ in children(b, ed, ed + es))
                   for ei, ed, es in children(b, cd, cd + cs)):
                return True
    return False


def mp4_encrypted_video(b, start=0, end=None, path=()):
    """True when MP4 boxes b[start:end] hold an encv sample entry whose sinf box holds a tenc box (ISO/IEC 23001-7)."""
    p, end = start, len(b) if end is None else end
    while p + 8 <= end:
        size, kind, head = int.from_bytes(b[p:p + 4], "big"), b[p + 4:p + 8], 8
        if size == 1:
            size, head = int.from_bytes(b[p + 8:p + 16], "big"), 16
        size = end - p if size == 0 else size
        if size < head or p + size > end:
            return False
        if kind == b"tenc" and {b"encv", b"sinf"} <= set(path):
            return True
        if kind in MP4_BOXES and mp4_encrypted_video(b, p + head + MP4_BOXES[kind], p + size, path + (kind,)):
            return True
        p += size
    return False


def remove_track(lines, late):
    """Whether a SubRip track with late of its lines starting after the real end is timed for another cut."""
    return late >= REMOVE_MIN and late >= REMOVE_SHARE * lines


def subtitle_plan(trim, remove, ends, streams, listed):
    """(trim, remove, why the file may be cut or None) for the late SubRip tracks header_probe() planned. ends maps a
    track to where it ends, streams is where the video and the audio end, both in seconds, listed is the runtime in
    minutes from the app or TMDB. A file whose video and audio end under CUT_END of the listed runtime may be cut, and
    nothing is trimmed or removed, whatever its tracks end at: one runaway line can lift a good track past REMOVE_END.
    So is a file with no runtime. Otherwise a planned removal stands only for a track that ends past
    REMOVE_END, and the others are trimmed."""
    if not listed:
        return [], [], "the subtitles run far past the video and audio, and no runtime is listed to tell whether the file is cut short"
    late, limit = sorted(set(trim) | set(remove)), listed * 60
    if streams < CUT_END * limit:
        i = min(late, key=ends.get)
        return [], [], (f"the video and audio stop at {clock(streams)}, but the listed runtime is {listed:g} minutes, and the subtitles run to "
                        f"{clock(ends[i])}")
    remove = sorted(i for i in remove if ends[i] > REMOVE_END * limit)
    return sorted(set(late) - set(remove)), remove, None


def trim_srt(text, limit):
    """(the SubRip text with every event cut at limit seconds, {"events", "cut", "dropped"}). An event that starts at or
    after limit goes, one that ends after it ends at it, and the events are numbered again. A block with no timing line
    is more text of the event before it, after a blank line, as mkvmerge reads it.
    Raises ValueError when the first block has no timing line, so a trim never drops text it could not read."""
    sec = lambda h, m, s, f: int(h) * 3600 + int(m) * 60 + int(s) + int(f.ljust(3, "0")) / 1000
    stamp = lambda t: f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{round(t % 1 * 1000) % 1000:03d}"
    events, out, n = [], [], {"events": 0, "cut": 0, "dropped": 0}
    for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").strip("\n\ufeff")):
        lines = block.split("\n")
        k = next((i for i, line in enumerate(lines[:2]) if SRT_TIME.match(line)), None)
        if k is None:
            if not events:
                raise ValueError(f"a SubRip block with no timing line: {block[:60]!r}")
            events[-1][1] += ["", *lines]
            continue
        events.append([SRT_TIME.match(lines[k]).groups(), lines[k + 1:]])
    for g, text_lines in events:
        start, end = sec(*g[:4]), sec(*g[4:8])
        n["events"] += 1
        if start >= limit:
            n["dropped"] += 1
            continue
        if end > limit:
            n["cut"] += 1
            end = limit
        out.append("\n".join([str(len(out) + 1), f"{stamp(start)} --> {stamp(end)}{g[8]}"] + text_lines))
    return "\n\n".join(out) + "\n", n


def stream_ends(b, durations):
    """{track number: the end of its last block in ticks} from b, the last bytes of a Matroska file, or None.

    durations maps a track number to its default duration in ticks. The walk starts at the first Cluster id from
    which every level-1 element up to the end of b parses, and every Cluster child with it. So a Cluster id inside
    frame data never counts. It needs no Cues, so it reads a file whose index is lost. None when no start holds, as
    when one Cluster is longer than b."""
    marker = CLUSTER.to_bytes(4, "big")
    at = b.find(marker)
    while at >= 0:
        ends, p, clusters = {}, at, 0
        while p < len(b):
            e = element(b, p)
            if not e:   # an element header cut by the end of the file
                p = len(b) if clusters and len(b) - p < 12 else -1
                break
            i, d, s = e
            end = len(b) if s is None else d + s
            if i not in LEVEL1 or (i == CLUSTER and not cluster_blocks(b, d, end, durations, ends)):
                p = -1
                break
            clusters += i == CLUSTER
            p = end
        if p >= 0 and clusters:
            return ends
        at = b.find(marker, at + 1)
    return None
