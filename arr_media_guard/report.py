# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The words of every decision. render() turns one decision record into one output. The outputs are the decision log
line, its logfmt summary for syslog, the Discord alerts and the CLI line. The tense is planned in a dry run and done in
an apply, so a dry run says what --apply would do.

The steps that find and act keep codes and facts only. A finding is {"kind": its code, **facts}, and its kind is the
alert kind. A finding the hook acted on holds the action, {"code": its code, **facts}. FINDINGS holds one template per
finding code, ACTIONS one per action code, and SUB_LINES one per sentence of a subtitle alert."""
import json, os, re

from . import cli, config, content, decide, logs, regrab, subsync

TENSES = ("planned", "done")
B, E = "\x02", "\x03"   # the ends of a bold span in the text of a template, see bold()
MARKDOWN = re.compile(r"([\\*_~`|])")   # the characters Discord reads as markdown, escaped in every embed
L, M, R = "\x05", "\x06", "\x07"   # the start, the middle and the end of a link span, see link()
LINK_TEXT = re.compile(r"([\\*_~`|\[\]])")   # the characters escaped in the text of a link: MARKDOWN and the brackets
SPAN = re.compile(f"{L}([^{M}]*){M}([^{R}]*){R}|{MARKDOWN.pattern}")   # a link span, or one markdown character, see escaped()
# a bold span or a link span, which never breaks, or the space after a sentence
SENTENCE_END = re.compile(f"{B}[^{E}]*{E}|{L}[^{R}]*{R}|(?<=[.!?])[ \n]+(?=[A-Z\"{B}{L}])")
GLUE = "\x04"   # a space after a full stop inside a fact, as in the name "PJ Robot Vs. Romeo". markdown() never breaks a line there.


def tense_of(rec):
    """The tense of rec: planned for a dry run, done for an apply and for a record with no apply field."""
    return "done" if rec.get("apply", True) else "planned"


def cap(s):
    i = int(s[:1] == B)
    return s[:i] + s[i:i + 1].upper() + s[i + 1:]


def bold(s):
    """s as a name or the key fact of an alert. The Discord embed bolds it, and every other output shows it plain."""
    return f"{B}{s}{E}"


def unmarked(text):
    """The text of a template for the decision log, the CLI and Loki: no bold."""
    return text.replace(B, "").replace(E, "").replace(GLUE, " ")


def glued(v):
    """The facts v with each space after a full stop in their text glued, see GLUE. So an embed breaks its lines only at
    the sentence ends of the template."""
    if isinstance(v, str):
        return re.sub(r"(?<=[.!?]) ", GLUE, v)
    if isinstance(v, dict):
        return {k: glued(x) for k, x in v.items()}
    return [glued(x) for x in v] if isinstance(v, list) else v


def link(text, url):
    """text as a link to url in a Discord embed, see escaped(). No url gives text alone. So does a text whose brackets
    do not pair, because Discord then ends the link early, escaped or not. Only the text of an embed holds a link, so the
    decision log, the CLI and Loki stay plain."""
    return f"{L}{text}{M}{url}{R}" if url and paired(text) else text


def paired(text):
    """Whether each "[" of text has its own "]" after it."""
    depth = 0
    for c in text:
        depth += (c == "[") - (c == "]")
        if depth < 0:
            return False
    return depth == 0


def escaped(text):
    """text with every Discord markdown character escaped, so a name never breaks the format. A link span of link()
    becomes [text](url). Its text has the brackets escaped too, and its URL stays as it is."""
    return SPAN.sub(lambda m: "\\" + m[3] if m[3] else "[" + LINK_TEXT.sub(r"\\\1", m[1]) + f"]({m[2]})", text)


def markdown(text):
    """The text of a template for a Discord embed, see logs.embed(). Every markdown character is escaped, and each bold
    span is in **. A text of more than two sentences gets one sentence a line."""
    lines = SENTENCE_END.sub(lambda m: m[0] if m[0][0] in (B, L) else "\n", text)
    text = lines if lines.count("\n") > 1 else text
    return escaped(text).replace(B, "**").replace(E, "**").replace(GLUE, " ")


def sentences(xs):
    """Phrases as sentences, each one capitalised and ended with a full stop."""
    return " ".join(f"{cap(x)}." for x in xs)


def and_list(xs):
    """Words as a person lists them, see content.and_join()."""
    return content.and_join(list(xs))


def amount(secs):
    """Seconds as a person says them: "1.7 s", "2 min 19 s"."""
    m, s = divmod(round(abs(secs)), 60)
    return f"{abs(secs):.1f} s" if abs(secs) < 60 else f"{m} min {s} s" if s else f"{m} min"


def late_by(offs):
    """The offsets of a subtitle from the audio, positive when it shows late: "2.5 s late", "1.2 s and 2.5 s late"."""
    if all(o > 0 for o in offs) or all(o <= 0 for o in offs):
        return f'{and_list(amount(o) for o in offs)} {"late" if offs[0] > 0 else "early"}'
    return and_list(late_by([o]) for o in offs)


def lang_word(code):
    """The language of a track as a player names it, "" for an untagged track."""
    return "" if code is None or code in decide.UNTAGGED else decide.lang_name(code)


def track_langs(rec):
    """{place: language} of the tracks of rec, by their place before any remux. The subtitle sentences name those places."""
    return {x["i"]: x["lang"] for x in (rec.get("subremux") or {}).get("tracks_before") or rec.get("tracks") or []}


def sub_name(p, langs):
    """A subtitle as a player shows it: "the English subtitles (track 2)". A sidecar goes by its file name."""
    if not re.fullmatch(r"s\d+", p):
        return f"the subtitles in {bold(p)}"
    word = lang_word(langs.get(p))
    return "the " + bold(f'{word + " " if word else ""}subtitles (track {p[1:]})')


def subs_name(ps, langs):
    """Subtitles as one subject: "subtitle tracks 1 and 2 (English)" for tracks of one language."""
    words = {lang_word(langs.get(p)) for p in ps}
    if len(ps) > 1 and len(words) == 1 and all(re.fullmatch(r"s\d+", p) for p in ps):
        word = words.pop()
        return bold(f"subtitle tracks {and_list(p[1:] for p in ps)}" + (f" ({word})" if word else ""))
    return and_list(sub_name(p, langs) for p in ps)


def default_track(x):
    """A default track after a failed flag edit, "a1 eng" (see process.after_edit()), as a player shows it."""
    pos, _, code = x.partition(" ")
    word = lang_word(code)
    return "the " + bold(f'{word + " " if word else ""}{"audio" if pos[:1] == "a" else "subtitles"} (track {pos[1:]})')


def after(f):
    """The file after a failed flag edit: does it still open, and which tracks play by default now."""
    if f.get("unread"):
        return f'The file no longer opens. {cap(f["unread"][:120])}'
    if f.get("on") is None:
        return ""
    on = and_list(default_track(x) for x in f["on"])
    return f"The file still opens, and its default tracks are {on}." if on else "The file still opens, and no track plays by default."


def failed_repack(f, t):
    if f.get("note"):   # a conversion that a stopped run left, see convert.pending_recover()
        return f'{cap(f["note"])}.'
    return f'Couldn\'t convert the {f["container"]} file to MKV, so the original was kept. {cap(f["why"])}.'


def damage(f, t):
    refused = f' The new MKV did not match the original, because {f["refusal"]}.' if f.get("refusal") else ""
    return f'Converting the file to MKV showed that it\'s damaged, because {bold(f["fault"])}. {f["line"].rstrip(".")}.{refused}'


# The subtitle formats a trim cannot cut, by their Matroska codec
CODEC_NAMES = {"S_TEXT/ASS": "ASS", "S_TEXT/SSA": "SSA", "S_HDMV/PGS": "PGS", "S_VOBSUB": "VobSub", "S_DVBSUB": "DVB", "S_TEXT/WEBVTT": "WebVTT"}


def overrun(f, t):
    """The subtitles of another format than SubRip that run past the end, see checks.header_probe(). A codec that
    CODEC_NAMES lacks is "a format"."""
    fmt = lambda c: f"{CODEC_NAMES[c]} format, which" if c in CODEC_NAMES else "a format that"
    return " ".join(f'{cap(sub_name(x["track"], f["langs"]))} keep going until {bold(content.hms(x["end"]))}, but the video and audio end at '
                    f'{content.hms(x["streams"])}. They\'re in {fmt(x["codec"])} can\'t be trimmed automatically, so they were left as they '
                    "are." for x in f["tracks"])


def sublang(f, t):
    n, muted = len(f["mismatch"]), len(f["muted"])
    return (f'{sentences(f["mismatch"])} Nothing else confirms another language, so the tag{"s were" if n > 1 else " was"} kept.'
            + ("" if not muted else f' Turned off {"their" if muted > 1 else "its"} default and forced flags.'))


def quote(title):
    """An episode title in quotes, in bold in the embed, see content.episode_why()."""
    return bold(f'"{title}"')


# The plain words of an edit result that a failed flag edit names, see process.edit()
EDIT_ERRORS = {"VERIFY FAILED, flags did not change": "the edit ran, but the flags did not change"}
# Per finding code: the alert title and the text. A fault with an action takes its title from FAULT_TITLES instead.
FINDINGS = {
    "language": ("Wrong audio language", lambda f, t: f'The audio is {bold(decide.lang_names(f["has"]) or "missing")}, but it should be {f["want"]}.'),
    "runtime": ("Wrong runtime", lambda f, t: f'The file runs {bold(f["runs"])}, but the listed runtime is {bold(str(f["listed"]) + " minutes")}.'),
    "duration": ("Wrong length in the file", lambda f, t: f["why"]),
    "episode": ("Maybe the wrong episode", lambda f, t: f'{cap(content.episode_why(f["imported"], f["said"], f["title"], f["names"], quote))}.'),
    "content": ("Wrong content", lambda f, t: sentences(f["signals"])),
    "audio": ("Audio may be broken", lambda f, t: f'{cap(f["certain"])}.' if f.get("certain") else sentences(f["doubts"])),
    "video": ("Video may be broken", lambda f, t: f'{cap(f["certain"])}.' if f.get("certain") else sentences(f["doubts"])),
    "damage": ("Damaged file", damage),
    "repack": ("Conversion to MKV failed", failed_repack),
    "header": ("File repair failed", lambda f, t: f'Couldn\'t repair the file, so it was left as it is. {cap(f["why"])}.'),
    "cut": ("File may be cut short", lambda f, t: f'{cap(f["why"])}. The file may be cut short, or the subtitles may belong to another version. '
                                                 "Nothing was changed."),
    "subtitle": ("Subtitles run past the end", overrun),
    "sublang": ("Subtitle language may be wrong", sublang),
    "edit": ("Track flag change failed", lambda f, t: f'Couldn\'t change which tracks play by default. '
                                                  f'{cap(EDIT_ERRORS.get(f["error"], f["error"]).rstrip("."))}. {after(f)}'.rstrip()),
    "policy": ("Policy file didn't load", lambda f, t: f'{bold(f["file"])} didn\'t load, so no tracks are changed until it\'s fixed. {f["error"]}'),
    "submatch": ("Wrong subtitles", lambda f, t: " ".join(sub_line(x, t, f["langs"]) for x in f["lines"])),
    "subtiming": ("Subtitles out of sync", lambda f, t: " ".join(sub_line(x, t, f["langs"]) for x in f["lines"])),
}
# The title of a certain fault the hook acted on. Its action code adds the end, see ACTIONS. Red, or amber when unconfirmed.
FAULT_TITLES = {"audio": "Broken audio", "content": "Wrong content", "video": "Broken video", "damage": "Damaged file"}
# Per fault kind: the result of a faulty file, of a clean one, and the words for the files a re-grab deleted.
FAULTS = {"audio": ("broken audio", "audio checked", "broken files"), "content": ("wrong content", "content checked", "files with the wrong content"),
          "video": ("corrupt video", "video checked", "corrupt files"), "damage": ("damaged source", "source checked", "damaged files")}
# The result of a wrong-content verdict per outcome code, so the result says what happened, see process.content_checks().
VERDICTS = {"wrong_content": "wrong content", "would_regrab": "would re-grab", "wrong_content_unconfirmed": "wrong content unconfirmed",
            "regrab_capped": "re-grab capped", "regrab_no_grab": "no grab record", "regrab_failed": "re-grab failed"}


def regrabbed(a, t):
    what = "the broken upgrade" if a.get("came") else "the broken file"
    if a.get("failed_before"):
        return f'Deleted {what}. Its download was already marked as failed, so {a["name"]} is already searching for another copy.'
    if a["n"] == 1:
        return f'Deleted {what} and marked the grab as failed, so {a["name"]} is searching for another copy.'
    return (f'Deleted {a["n"]} {FAULTS[a["kind"]][2]} from this download and marked the grab as failed, so {a["name"]} is searching for '
            "other copies.")


# The faults each REGRAB kind re-grabs, for the text of a kind REGRAB leaves out
REGRAB_WORDS = {"audio": "broken audio", "video": "broken video", "content": "wrong content", "damage": "damaged files"}
# Per action code: the end of a fault's alert title, and the text. Every text ends with the restore, see restored().
ACTIONS = {
    "regrabbed": (", re-grabbed", regrabbed),
    "searched": (", re-grabbed", lambda a, t: f'Deleted the broken file and asked {a["name"]} to search for another copy. It was a manual import, '
                                              "so there was no grab to mark as failed."),
    "restored": (", old file restored", lambda a, t: "Deleted the broken file. It was a manual import, so there was no grab to mark as failed, "
                                                     f'and {a["name"]} won\'t search for another copy.'),
    "deleted": ("", lambda a, t: "Deleted the broken file. Its item isn't monitored, so no search was started."),
    "would_regrab": (", re-grab is off", lambda a, t: f'Re-grabs for {REGRAB_WORDS[a["kind"]]} are off, so the file was kept.'),
    "unconfirmed": (", not confirmed", lambda a, t: "A second check didn't find the same problem, so the file was kept."),
    "capped": ("", lambda a, t: f'The limit of {a["cap"]} re-grabs a day was reached, so the file was kept.'),
    "no_grab": ("", lambda a, t: f'{a["name"]} has no record of grabbing it, so the file was kept.'),
    "failed": ("", lambda a, t: f'The {"restore" if a.get("manual") else "re-grab"} failed during {a["step"]}. {a["error"]}'[:300]),
    "dry_run": ("", lambda a, t: "Dry run, so nothing was changed."),
    "no_policy": ("", lambda a, t: f'Skipped {bold(a["file"])}.'),
}


def restored(a, text):
    """text with the restore of a re-grab after it, see regrab.restore_facts(): the job's own old file, then the other
    files of the download, then why the job's own old file stayed out."""
    came = a.get("came") or []
    if came:
        them = "it" if len(came) == 1 else "them"
        text += (f' Put back the old file{"s" if len(came) > 1 else ""} from '
                 f'{("the kept cop" + ("ies" if len(came) > 1 else "y")) if a["own_copy"] else "the recycle bin"}: {", ".join(map(bold, came))}. '
                 + (f'{a["name"]} picked {them} up again.' if a["linked"]
                    else f'{a["name"]} didn\'t pick {them} up within {regrab.RESTORE_WAIT} seconds.'))
    if a.get("others"):
        s = "s" if a["others"] > 1 else ""
        text += f' Also put back the old file{s} of {a["others"]} more broken file{s} from this download.'
    if a.get("stayed") and not came:
        text += f' The old file wasn\'t put back, because {a["stayed"]}.'
    return text


def action_code(a):
    """The code that ends a fault's alert title: restored when a re-grab or a restore put the job's own old file back,
    deleted when a restore put none back."""
    if a["code"] in ("regrabbed", "restored") and a.get("came"):
        return "restored"
    return "deleted" if a["code"] == "restored" else a["code"]


def title(f):
    """(title, color) of the alert of finding f."""
    a = f.get("action")
    if f["kind"] in FAULT_TITLES and a:
        return FAULT_TITLES[f["kind"]] + ACTIONS[action_code(a)][0], "amber" if a["code"] == "unconfirmed" else "red"
    if f["kind"] == "repack" and f.get("note"):   # a conversion that a stopped run left
        return "Stopped conversion", "amber"
    return FINDINGS[f["kind"]][0], "amber"


# The fixes that go to the decision log only, see posts(). The action codes of a fault the program re-grabbed or whose
# old file it put back, and the sentence codes of a subtitle it removed or left out of a conversion.
LOGGED_ACTIONS = ("regrabbed", "restored", "searched")
LOGGED_LINES = ("removed", "converted_track")
COVERS_LENGTH = ("header", "cut", "subtitle")   # the findings whose alert names the cause of a wrong length in the file
DELETED = LOGGED_ACTIONS + ("deleted",)   # the action codes of a fault whose file the program deleted
CONTENT_SIGNALS = ("language", "runtime")   # the findings that share their kind with a signal of the wrong-content evidence


def fixed(a):
    """Whether the action a fixed its fault, see LOGGED_ACTIONS. A re-grab or a search fixes it also when its own old
    file stayed out, because the app searches for another copy. A restore of a manual import fixes it only when its own
    old file came back. An old file that came back counts only when the app picked it up, see regrab.restore_facts()."""
    came = a.get("came") or []
    if a.get("code") not in LOGGED_ACTIONS or (a["code"] == "restored" and not came):
        return False
    return not came or bool(a.get("linked"))


def posts(f, rec=None):
    """Whether the alert of finding f of the record rec goes to Discord, see logs.alert_findings(). A problem the program
    left unresolved posts: a failed fix, a fix a setting turned off, a fix that cannot run, and a doubt. A problem it
    fixed goes to the decision log only, see fixed(). A subtitle finding posts when one of its sentences does. A moved
    sidecar counts as fixed.

    One cause posts once. When the program deleted the file, its other findings log only. A wrong-content finding names
    each signal that scored, see process.content_finding(). A language or runtime finding logs only beside it when it
    names that signal. The episode signal scores no points, so the episode finding posts. A wrong length in the file
    posts only when no repair fixed it and no alert of COVERS_LENGTH names its cause."""
    found = (rec or {}).get("findings") or []
    if f.get("action"):
        return not fixed(f["action"])
    if any((x.get("action") or {}).get("code") in DELETED for x in found):
        return False
    if f["kind"] in CONTENT_SIGNALS and any(x["kind"] == "content" and f["kind"] in x.get("scored", ()) for x in found):
        return False
    if f["kind"] == "duration":
        return ((rec or {}).get("header_repair") or {}).get("code") not in config.REPAIRED and not any(x["kind"] in COVERS_LENGTH for x in found)
    if "lines" in f:
        return any(x["code"] not in LOGGED_LINES and not (x["code"] in ("sidecar", "converted_sidecar") and x.get("kept")) for x in f["lines"])
    return True


def texts(f, t, langs=None, marked=False):
    """(text, action text or None) of finding f. langs names the language of each track, see track_langs(). The texts
    are plain, or with their bold spans when marked, for the embed, see markdown(). A template that fails gives a line
    that says so, because an alert text must never cost the decision line or the post."""
    try:
        f = glued(f) if marked else f
        a = f.get("action")
        text, act = FINDINGS[f["kind"]][1](dict(f, langs=langs or {}), t), restored(a, ACTIONS[a["code"]][1](a, t)) if a else None
        return (text, act) if marked else (unmarked(text), act and unmarked(act))
    except Exception as ex:
        return config.mask(f"no text: {type(ex).__name__}: {ex}")[:200], None


# What --apply would do with the subtitle remux a dry run planned, per code of subtitles.remux_block()
BLOCKS = {
    "remux": lambda b: "--apply would remux the file.",
    "hardlinked": lambda b: "--apply would leave the file as it is, because it has another hard link, such as the download client's copy. Run "
                            "--apply again after the other link is gone, for example after the download client removes its copy.",
    "cap": lambda b: f'--apply would skip the remux, because the file is {b["why"]}. Raise REPACK_MAX_GB to remux it.',
    "space": lambda b: f'--apply would skip the remux, because {b["folder"]} has {b["free"]:.1f} GB free, and the remux needs {b["need"]:.1f} GB '
                       f'there. Free space in {b["folder"]}.',
    "keep_root": lambda b: f'--apply would skip the remux, because {b["user"]} cannot write to {b["root"]}, where it keeps the original. Make '
                           f'{b["root"]} writable for them. In Docker, PUID and PGID set them.',
    "keep_create": lambda b: f'--apply would skip the remux, because {b["user"]} cannot create {b["root"]}, where it keeps the original. Create '
                             f'{b["root"]}, writable for {b["user"]}. In Docker, PUID and PGID set them.',
    "keep": lambda b: f'--apply would skip the remux, because it cannot keep the original. {cap(b["why"])}.',
}


def block(b):
    return BLOCKS[b["code"]](b)


# Why a track that does not match stays in the file, per kept_back code of process.subtitle_checks()
KEPT_BACK = {"check": "SUBTITLES is set to check",
             "keep_days": "KEEP_ORIGINALS_DAYS is 0, and a removal needs a copy of the original"}


def remux_why(result):
    """The plain reason of a subtitle remux that was skipped or failed, from its result, see remux.resub(). Another
    text stays as it is."""
    skip = (result or "").removeprefix("subtitle remux skipped, ")
    if skip.startswith("hardlinked"):
        return "the file has another hard link, such as the download client's copy"
    if m := re.match(r"over the (\d+) GB", skip):
        return f"the file is over the {m[1]} GB limit of REPACK_MAX_GB"
    if m := re.match(r"low space: ([\d.]+) GB free for ([\d.]+) GB", skip):
        return f"{bold(f'only {m[1]} GB is free')} for the {m[2]} GB file"
    if skip.startswith("the original cannot be kept"):
        return "no copy of the original could be kept"
    if skip.startswith("subtitle remux failed, the original changed"):
        return "the app replaced or renamed the file at the same time"
    if skip.startswith("subtitle remux failed: "):
        return f'rewriting the file failed ({skip.removeprefix("subtitle remux failed: ")})'
    return skip


def stays(x, t):
    """The end of the sentence of a track that does not match and stays in the file."""
    planned = t == "planned"
    flags = ("--apply would turn their default and forced flags off." if planned else "Turned off their default and forced flags.") \
        if x["flags_off"] else "Their flags were left as they are."
    if planned and x.get("hardlinked"):   # edit() and the remux both leave a hardlinked file as it is
        return block({"code": "hardlinked"})
    if planned and not x["gone"] and x.get("kept_back") == "keep_days":
        return ("They would stay in the file, because a removal keeps the original, and KEEP_ORIGINALS_DAYS is 0. Set "
                f"KEEP_ORIGINALS_DAYS above 0 to remove them. {flags}")
    if planned and x["gone"] and x.get("block"):
        return "--apply would remove them." if x["block"]["code"] == "remux" else f'{block(x["block"])} They would stay in the file. {flags}'
    why = remux_why(x["result"]) if x["gone"] else KEPT_BACK.get(x.get("kept_back"), "the run could not remove them")
    return f"They're still in the file, because {why}. {flags}"


def where(x):
    return f'Moved the file to {x["kept"]}.' if x.get("kept") else f'The file was left beside the video, because {x.get("left") or "this is a dry run"}.'


def off_line(x, t):
    """A subtitle whose times are off and stay: off by different amounts in parts of the file, or by an offset that no
    fix lines up, see subsync.fit()."""
    vs = f' compared with {sub_name(x["ref"], x["langs"])}' if x.get("ref") else ""
    name = cap(sub_name(x["track"], x["langs"]))
    if x.get("offsets"):
        return (f'{name} are out of sync{vs} by different amounts in different parts of the file: {bold(late_by(x["offsets"]))}. One shift can\'t '
                "fix that, so they were left as they are.")
    seem = f' seem {bold("about " + late_by([x["unfixed"]]))}{vs}' if x.get("unfixed") is not None else f" are out of sync{vs}"
    return f"{name}{seem}, but no fix lined them up well enough, so they were left as they are."


def check_times(x, t):
    """A subtitle whose times need a fix that SUBTITLES check leaves out."""
    fix = x.get("fix")
    off = (f'are {bold("about " + late_by([fix["offset"]]))}' + ("" if fix["rate"] == "1/1" else " at the start and drift over time")) if fix else "are out of sync"
    return f'{cap(sub_name(x["track"], x["langs"]))} {off}. SUBTITLES is set to check, so they were left as they are.'


def sweep_line(x, t):
    """The parts of the file where the sweep heard a subtitle far off its fitted line. Tracks off at the same parts share
    one sentence. Offsets that agree give one mean."""
    rows, groups = {}, {}
    for k, at, off in x["far"]:
        rows.setdefault(k, []).append((at, off))
    for k, r in rows.items():
        groups.setdefault(tuple(r), []).append(k)
    out = []
    for r, ks in groups.items():
        offs, name = [o for _, o in r], cap(subs_name(ks, x["langs"]))
        mean = sum(offs) / len(offs)
        if max(offs) - min(offs) <= max(0.5, 0.05 * abs(mean)) and all(o * mean > 0 for o in offs):
            out.append(f'{name} are {bold("about " + late_by([mean]))} at {and_list(content.hms(a) for a, _ in r)}.')
        elif all(o * mean > 0 for o in offs):
            out.append(f'{name} are {"late" if mean > 0 else "early"} by {and_list(f"{amount(o)} at {content.hms(a)}" for a, o in r)}.')
        else:
            out.append(f'{name} are out of sync: {and_list(f"{late_by([o])} at {content.hms(a)}" for a, o in r)}.')
    return " ".join(out) + " They were left as they are."


NO_MATCH = "don't match what's said in the audio"
# How a sidecar that stays as it is needs new times, per action of subtitles.sidecar_fix()
SIDECAR_NEEDS = {"retime": "are out of sync", "lengthen": "flash by too fast to read"}
# Per sentence code of a subtitle alert, its text. subtitles.sub_findings() gives the codes and the facts, and sub_line() the track languages.
SUB_LINES = {
    "removed": lambda x, t: f'{cap(sub_name(x["track"], x["langs"]))} {NO_MATCH}. Removed them and kept the original file at {x["kept"]}.',
    "stays": lambda x, t: f'{cap(sub_name(x["track"], x["langs"]))} {NO_MATCH}. {stays(x, t)}',
    "sidecar": lambda x, t: f'{cap(sub_name(x["name"], x["langs"]))} {NO_MATCH}. {where(x)}',
    "converted_sidecar": lambda x, t: f'{cap(sub_name(x["name"], x["langs"]))} {NO_MATCH}, so the conversion to MKV left them out. {where(x)}',
    "converted_track": lambda x, t: f'Subtitle track {x["track"][1:]} of the original file doesn\'t match what\'s said in the audio, so the '
                                    f'conversion to MKV left it out. The original file is kept at {x["kept"]}.',
    "sidecar_left": lambda x, t: f'{cap(sub_name(x["name"], x["langs"]))} {SIDECAR_NEEDS.get(x.get("action"), "need new times")}, but the file '
                                 f'was left as it is, because {x["left"]}.',
    "off": off_line,
    "not_retimed": lambda x, t: f'{cap(subs_name(x["tracks"], x["langs"]))} need new times'
                                + (f'. {block(x["block"])}' if t == "planned" else
                                   f', but the fix failed, because {remux_why(x["result"])}. The file was left as it is.'),
    "check_times": check_times,
    "check_flash": lambda x, t: f'{cap(sub_name(x["track"], x["langs"]))} flash by too fast to read. Half the lines show for {x["median"]:.2f} s '
                                "or less. SUBTITLES is set to check, so they were left as they are.",
    "sweep": sweep_line,
}


def sub_line(x, t, langs=None):
    """One sentence of a subtitle alert. langs names the language of each track, see track_langs()."""
    return SUB_LINES[x["code"]](dict(x, langs=langs or {}), t)


def alert_line(f, t, langs=None):
    """One alert of the decision log and the CLI: "kind: text action"."""
    text, act = texts(f, t, langs)
    return f'{f["kind"]}: {text}' + (f" {act}" if act else "")


# What a failed TMDB answer means for the language check, for the footer of the alerts it bears on
TMDB_SKIPPED = {"no_record": "TMDB has no record of this item", "tmdb_unavailable": "TMDB didn't answer", "tmdb_token_missing": "No TMDB key is set",
                "tmdb_token_rejected": "TMDB rejected the key"}


def alert_embed(rec, f, t):
    """The Discord embed of finding f of rec: the problem and what the hook did, one field with the title and the file,
    and the footer with the host. A language or content alert names a TMDB failure there, because TMDB's language check
    did not run. When the item has a page in its app, the field shows the item's name as a link to it, on its own line
    above the file. The field name is then blank, because Discord shows no link in a field name. With no link, the field
    name is the item's name, as before."""
    (text, act), (head, color) = texts(f, t, track_langs(rec), marked=True), title(f)
    note = f'{TMDB_SKIPPED[rec["tmdb"]]}, so its language check was skipped' if f["kind"] in ("language", "content") \
        and rec.get("tmdb") in TMDB_SKIPPED else None
    shown, file = link(rec["label"], logs.page(rec)), os.path.basename(rec["path"])
    item = (rec["label"], file) if shown == rec["label"] else ("\u200b", f"{shown}\n{file}")   # U+200B, a zero-width space
    return logs.embed(rec["app"], head, f"{text}\n{act}" if act else text, color, [item],
                      " · ".join(x for x in (note, f"{config.CFG.name} on {config.CFG.instance}") if x))


def logfmt(pairs):
    """key=value pairs for Loki's logfmt parser. A value with a space or a quote is quoted. The app key is "arr", because
    Loki's app label already holds the syslog tag."""
    out = []
    for k, v in pairs:
        v = "" if v is None else str(v)
        out.append(f"{k}={json.dumps(v, ensure_ascii=False) if not v or re.search(r'[\s\"=]', v) else v}")
    return " ".join(out)


def render(rec, target, tense=None):
    """One output of the decision record rec, in tense, by default the tense of rec, see tense_of().

    log: the decision line, with the schema, the script and policy versions, the outcome code (other when rec has
    none), the seconds it took from rec["took"], and the text of each finding in alerts.
    logfmt: its one-line summary for syslog. Loki reads its keys, so they stay. An error line ends with error, its
    result cut to 150 characters.
    embed: one Discord embed per finding.
    cli: the line of a backfill."""
    t = tense or tense_of(rec)
    if t not in TENSES:
        raise ValueError(f"no tense {t}")
    if target == "log":
        out = dict({k: v for k, v in rec.items() if k not in ("outcome", "took")}, schema=config.SCHEMA, version=config.VERSION,
                   policy=logs.policy_hash(), host=config.HOST, outcome=rec.get("outcome", "other"), took=rec.get("took"))
        if "findings" in rec:
            out["alerts"] = [alert_line(f, t, track_langs(rec)) for f in rec["findings"]]
        return out
    if target == "logfmt":   # an error adds its result, and a line with no label names its file or its job
        label = rec.get("label") or (os.path.basename(rec["path"]) if rec.get("path") else rec.get("job"))
        error = [("error", config.mask(rec.get("result") or "")[:150])] if rec.get("outcome") == "error" else []
        return config.mask(logfmt([("arr", rec.get("app")), ("source", rec.get("source")), ("outcome", rec.get("outcome", "other")),
                                   ("class", rec.get("class")), ("edits", len(rec.get("edits") or [])), ("reasons", ",".join(rec.get("reasons") or [])),
                                   ("alerts", ",".join(rec.get("alert_kinds") or [])), ("tmdb", rec.get("tmdb") or "not_asked"),
                                   ("label", label), ("id", rec.get("id"))] + error))
    if target == "embed":
        return [alert_embed(rec, f, t) for f in rec.get("findings") or []]
    if target == "cli":
        return cli_line(rec, t)
    raise ValueError(f"no target {target}")


def edit_text(pos, edits):
    """A plan for a printed line: "a1 1->0, s1 flag-forced 1->0". pos maps a selector to the track position."""
    return ", ".join(f"{pos.get(e[0], e[0])} {e[3] + ' ' if len(e) > 3 else ''}{e[2]}->{e[1]}" for e in edits)


def remux_column(rec, t):
    """The subtitle remux of rec for a report column. A dry run says what --apply would do."""
    rm = rec.get("subremux") or {}
    if t == "done" or not rm.get("codes"):
        return rm.get("result") or ""
    return "--apply would remux" if "would_remux_subtitles" in rm["codes"] else "--apply would skip the remux"


def sub_text(rec, t):
    """The subtitle column of a backfill line: each checked track's verdict, overlaps and timing."""
    out = []
    for p, r in sorted((rec.get("subcheck") or {}).items()):
        laps = "/".join(f'{w["overlap"]:.0%}' for w in r.get("windows") or [])
        fix = (r.get("timing") or {}).get("fix")
        out.append(f'{p} {r["verdict"]}{" " + laps if laps else ""}' + (f' fix {fix["offset"]:+.2f} s {fix["rate"]}' if fix else "")
                   + ("" if r["verdict"] == "match" else f' ({r["why"]})')
                   + (f' (times stay: {x["why"]})' if (x := r.get("timing") or {}).get("piecewise") or "unfixed" in x or "unconfirmed" in x else ""))
    out += [f'{k} flashes, {f["lengthened"]} of {f["cues"]} ends ' + ("need a fix, WebVTT: report only" if f.get("report_only") else "lengthened")
            for k, f in sorted((rec.get("flash") or {}).items())]
    out += [f'{e["name"]}: {e["action"]} {e["result"]}' for e in rec.get("sidecars") or []]
    out += [f'whole-file read of {", ".join(f["tracks"])} in {f["took"]} s' + (f', {f["why"]}' if f.get("why") else "") for f in [rec.get("full_read")] if f]
    r = remux_column(rec, t)[:150]
    return "; ".join(out) + (f" | {r}" if r else "")


def cli_line(rec, t):
    """The line of a backfill for one file: the result, the label, the plan, then what the run heard, repaired, checked,
    forced and alerted."""
    pos = {x["sel"]: x["pos"] for x in rec.get("before", [])}
    rp = rec.get("repack") or {}
    heard = ", ".join(f'{k} {v["lang"] or "no answer"}' for k, v in (rec.get("heard") or {}).items())
    return (f'{rec["result"][:60]:<12} {rec.get("label")}' + (f" | {e}" if (e := edit_text(pos, rec.get("edits", []))) else "")
            + (" | heard " + heard if heard else "")
            + (" | header " + rec["header_repair"]["result"][:150] if "header_repair" in rec else "")
            + (" | subtitles " + sub_text(rec, t) if rec.get("subcheck") or rec.get("flash") else "")
            + (" | forced, the proof refused: " + rp["forced"][:150] if rp.get("forced") else "")
            + (" | not forced: " + rp["not_forced"][:150] if rp.get("not_forced") else "")
            + (" | name forced: " + rp["forced_name"][:150] if rp.get("forced_name") else "")
            + (" | " + "; ".join(rec["notes"]) if rec.get("notes") else "")
            + (" | ALERT " + "; ".join(alert_line(f, t, track_langs(rec)) for f in rec["findings"]) if rec.get("findings") else ""))


def sub_time_report(rec, t):
    """The report of --sub-time on one file: its result, then one line per subtitle with its place or sidecar name,
    codec, language, role, method, verdict, offset and ratio, and action, then the rows of the sweep, the blocks of the
    dense hearing and the parts it left, and the alerts."""
    rm, done = rec.get("subremux") or {}, {e["name"]: e for e in rec.get("sidecars") or []}
    pending = remux_column(rec, t)[:100]   # the action of a track the remux planned for
    tracks = {x["i"]: x for x in rm.get("tracks_before") or rec.get("tracks") or []}   # the places the check named
    stem = os.path.splitext(os.path.basename(rec["path"]))[0]
    tags = lambda n: n[len(stem) + 1:-4].lower().split(".")   # a sidecar's name gives its language and flags
    out = [f'{rec["result"][:80]} | {rec.get("label")} | {rec["path"]}']
    if rm.get("done") and rm.get("kept"):
        out.append(f'  original kept at {rm["kept"]}, {rm.get("kept_how") or "hard-linked"}')
    for p, r in [*(rec.get("subcheck") or {}).items(), *(rec.get("subtime") or {}).items()]:
        x, info = r.get("timing") or {}, tracks.get(p) or {}
        fix = x.get("fix")
        when = f'{fix["offset"]:+.2f} s {fix["rate"]}' if fix else "in time" if x.get("why") == "in time" else "-"
        if p in done:
            act = f'sidecar {done[p]["result"]}' + (f': {done[p]["left"]}' if done[p].get("left") else "")
        elif p in (rm.get("remove") or []) or p in (rm.get("fixed") or []) or p in (rm.get("timed") or []):
            act = ("removed" if p in (rm.get("remove") or []) else "retimed" if p in (rm.get("fixed") or []) else "blocks moved") if rm.get("done") else pending
        elif r["verdict"] == "mismatch":
            act = "flags off" if not r.get("held") else "none, held"
        elif r["verdict"] == "weak":
            act = "report only"
        else:
            act = "times stay" if fix or x.get("piecewise") or "unfixed" in x or "unconfirmed" in x else "none"
        why = x.get("why") if x and x.get("why") != "in time" else r.get("why")
        verdict = r["verdict"] + (f' {r["score"]:.2f}' if r.get("score") is not None else "")
        method = ("words" + (f', a reference: {rec["references"][p]}' if p in (rec.get("references") or {}) else "")) if p not in (rec.get("subtime") or {}) \
            else f'reference {r["reference"]}' if r.get("reference") else "reference, none"
        lang, role = (info.get("lang"), info.get("role")) if info else (tags(p)[0], "sdh" if {"hi", "sdh", "cc"} & set(tags(p)) else "full")
        out.append(f'  {p} | {info.get("codec") or r.get("codec") or ("?" if info else "srt")} | {r.get("lang") or lang} | {r.get("role") or role}'
                   f' | {method} | {verdict} | {when} | {act} | {why}')
    for k, f in sorted((rec.get("flash") or {}).items()):
        act = "report only, WebVTT" if f.get("report_only") else f'sidecar {done[k]["result"]}' if k in done \
            else ("lengthened" if rm.get("done") else pending) if k in (rm.get("ended") or []) else "ends stay"
        out.append(f'  {k} | flash | median {f["median"]:.2f} s | {f["lengthened"]} of {f["cues"]} ends lengthened | {act} | first '
                   + ", ".join(f"{a:.3f} {o:.3f}->{n:.3f}" for a, o, n in f["first"]))
    for k, rows in sorted((rec.get("sweep") or {}).items()):
        out.append(f"  sweep of {k}: {len(rows)} windows, {sum(w['overlap'] >= subsync.MATCH for w in rows)} match")
        steps, alerts = cli.sweep_steps(rows), cli.sweep_alerts(rows)
        share = f"in a step of {len(steps)} of the {len(cli.sweep_trusted(rows))} windows that heard {subsync.MIN_CUES} cues"
        out += [f'    {content.hms(w["at"])} words {w["words"]} overlap {w["overlap"]:.0%} cues {w["cues"]} offset '
                + ("-" if w["offset"] is None else f'{w["offset"]:+.2f} s')
                + (f' ALERT {w["off"]:+.2f} s off the fitted line' if id(w) in alerts else
                   f' {w["off"]:+.2f} s off the fitted line, ' + (share if id(w) in steps else "one window alone")
                   if cli.sweep_far(w) else "") for w in rows]
    for k, b in sorted((rec.get("blocks") or {}).items()):   # a block moves in the remux or in the sidecar's rewrite, see process.subtitle_checks()
        planned = k in (rm.get("timed") or []) or k in done
        landed = (k in (rm.get("timed") or []) and rm.get("done")) or (done.get(k) or {}).get("result") == "retimed"
        verb = "moved" if landed else "would move" if planned and t == "planned" else "not moved"
        out += [f'  {k}: {x["cues"]} cues from {content.hms(x["from"])} to {content.hms(x["to"])} {verb} {-x["shift"]:+.2f} s, '
                f'{subsync.AGREE:.0%} of {x["anchors"]} heard cues agree within {x["spread"]:.2f} s{onset_text(x.get("onsets"))}' for x in b["blocks"]]
        out += [f'  {k}: no block from {content.hms(x["lo"])} to {content.hms(x["hi"])}, {why}' for x in b["parts"] if x.get("why")
                for why in dict.fromkeys(x.get("whys") or [x["why"]])]   # every reason the part moved nothing, once each
    if rec.get("full_read"):
        f = rec["full_read"]
        out.append(f'  whole-file read of {", ".join(f["tracks"])}: {f["took"]} s, {f["cpu"]} CPU s' + (f'; {f["why"]}' if f.get("why") else ""))
    if rec.get("unindexed"):
        out.append(f'  not read: {", ".join(rec["unindexed"]["tracks"])}, {rec["unindexed"]["why"]}')
    if rec.get("sweep_facts"):
        f = rec["sweep_facts"]
        cost = "sweep: words from the cache" if f.get("runs") and f.get("cached") == f["runs"] else f'sweep cost: {f["cpu"]} CPU s, {f["took"]} s'
        out.append(f'  {cost}' + "".join(f"; no words from {x}" for x in f["failed"]))
        if f.get("dense"):
            g = f["dense"]
            cache = "; words from the cache" if g.get("runs") and g.get("cached") == g["runs"] else ""
            out.append(f'  dense hearing of {g["windows"]} windows and the speech onsets: {g["cpu"]} CPU s, {g["took"]} s{cache}'
                       + "".join(f"; no words from {x}" for x in g["failed"]) + "".join(f"; no speech onsets: {x}" for x in g.get("onset_why") or []))
    out += [f"  ALERT {alert_line(f, t, track_langs(rec))}" for f in rec.get("findings") or []]
    return "\n".join(out)


def onset_text(o):
    """What the speech onsets said of a block, see subsync.blocks(): they agree with Whisper, or there are too few, and
    Whisper alone moves it from BLOCK_ALONE seconds."""
    if not o:
        return ""
    if o.get("verdict") == "agree":
        return f', speech onsets agree ({o["inside"]} in the block, {o["outside"]} around it)'
    return f", few speech onsets, Whisper alone at {subsync.BLOCK_ALONE} s"


def missed(rec):
    """Why a change that --sub-time --apply planned for the file of rec did not happen, or None: an error, a flag edit
    that failed or was not made, a subtitle remux that failed or was skipped, or a sidecar left as it was."""
    rm, hr = rec.get("subremux") or {}, rec.get("header_repair") or {}
    if rec.get("outcome") in ("error", "edit_failed", "verify_failed", "hardlinked", "read_only"):
        return rec["result"][:200]
    if hr.get("code") in ("header_repair_failed", "header_repair_skipped"):
        return hr["result"][:200]
    if (rm.get("fixed") or rm.get("ended") or rm.get("timed") or rm.get("remove")) and not rm.get("done"):
        return rm.get("result", "the subtitle remux did not run")[:200]
    left = [e["name"] for e in rec.get("sidecars") or [] if e.get("result") == "left"]
    return f'the sidecar {", ".join(left)} stays as it was' if left else None
