# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The words of every decision. render() turns one decision record into one output. The outputs are the decision log
line, its logfmt summary for syslog, the Discord alerts and the CLI line. The tense is planned in a dry run and done in
an apply, so a dry run says what --apply would do.

The steps that find and act keep codes and facts only. A finding is {"kind": its code, **facts}, and its kind is the
alert kind. A finding the hook acted on holds the action, {"code": its code, **facts}. FINDINGS holds one template per
finding code, ACTIONS one per action code, and SUB_LINES one per sentence of a subtitle alert."""
import json, os, re

from . import cli, config, content, logs, regrab, subsync

TENSES = ("planned", "done")


def tense_of(rec):
    """The tense of rec: planned for a dry run, done for an apply and for a record with no apply field."""
    return "done" if rec.get("apply", True) else "planned"


def cap(s):
    return s[:1].upper() + s[1:]


def sentences(xs):
    """Phrases as sentences, each one capitalised and ended with a full stop."""
    return " ".join(f"{cap(x)}." for x in xs)


def after(f):
    """The file after a failed flag edit: does it still read, and which tracks are default now."""
    if f.get("unread"):
        return f'The file no longer reads: {f["unread"][:120]}'
    if f.get("on") is None:
        return ""
    return f'The file still reads, and its default tracks are {", ".join(f["on"]) or "none"}.'


def failed_repack(f, t):
    if f.get("note"):   # a conversion that a stopped run left, see convert.pending_recover()
        return f'{cap(f["note"])}.' + ("" if f["state"] == "converted" else " Check these files by hand.")
    return f'The repack into Matroska failed, so the original {f["container"]} file stays. {cap(f["why"])}.'


def damage(f, t):
    refused = f' The proof refused the new file, because {f["refusal"]}.' if f.get("refusal") else ""
    return f'The conversion found a damaged source, because {f["fault"]}. {f["line"].rstrip(".")}.{refused}'


# Per finding code: the alert title and the text. A fault with an action takes its title from FAULT_TITLES instead.
FINDINGS = {
    "language": ("Wrong language", lambda f, t: f'No audio track is {f["want"]}. The file has {", ".join(f["has"])}.'),
    "runtime": ("Wrong runtime", lambda f, t: f'It runs {f["runs"]}, but the listed runtime is {f["listed"]} minutes.'),
    "duration": ("Broken duration header", lambda f, t: f["why"]),
    "episode": ("Wrong episode", lambda f, t: f'{cap(f["why"])}. Check the series\' episode order in Sonarr, or import the file to {f["names"]} by hand.'),
    "content": ("Wrong content", lambda f, t: " ".join(f"{cap(w)}." for w in f["signals"])
                + f' That is {f["points"]} points, and a re-grab needs {content.REGRAB_POINTS}.'),
    "audio": ("Audio check uncertain", lambda f, t: f'{cap(f["certain"])}.' if f.get("certain") else sentences(f["doubts"])),
    "video": ("Video check uncertain", lambda f, t: f'{cap(f["certain"])}.' if f.get("certain") else sentences(f["doubts"])),
    "damage": ("Damaged source", damage),
    "repack": ("Repack failed", failed_repack),
    "header": ("Header repair failed", lambda f, t: f'The header repair failed, so the file stays as it was. {cap(f["why"])}.'),
    "cut": ("File may be cut", lambda f, t: f'A subtitle runs far past the video and the audio, but {f["why"]}. The file may be cut, or the subtitle '
                                            "may belong to another episode or cut. It stays as it is, subtitles included. Check whether the video "
                                            "ends on the credits."),
    "subtitle": ("Subtitle runs past the end", lambda f, t: f'{sentences(f["issue"])} Subtitle track {", ".join(f["tracks"])} is not SubRip, so the '
                                                            "hook cannot cut it."),
    "sublang": ("Subtitle language", lambda f, t: f'{sentences(f["mismatch"])} Nothing else backs a new tag, so the tag stays. '
                                                  f'{sentences(f["muted"])}'.rstrip() + " Check the track and fix its tag."),
    "edit": ("Flag edit failed", lambda f, t: f'The flag edit failed. {f["error"]} {after(f)}'),
    "policy": ("Policy did not load", lambda f, t: f'{f["file"]} did not load, so the hook edits nothing. {f["error"]}'),
    "submatch": ("Wrong subtitle", lambda f, t: " ".join(sub_line(x, t) for x in f["lines"])),
    "subtiming": ("Subtitle timing", lambda f, t: " ".join(sub_line(x, t) for x in f["lines"])),
}
# The title of a certain fault the hook acted on. Its action code adds the end, see ACTIONS. Red, or amber when unconfirmed.
FAULT_TITLES = {"audio": "Broken audio", "content": "Wrong content", "video": "Corrupt video", "damage": "Damaged source"}
# Per fault kind: the result of a faulty file, of a clean one, and the words for the files a re-grab deleted.
FAULTS = {"audio": ("broken audio", "audio checked", "broken files"), "content": ("wrong content", "content checked", "files with the wrong content"),
          "video": ("corrupt video", "video checked", "corrupt files"), "damage": ("damaged source", "source checked", "damaged files")}
# The result of a wrong-content verdict per outcome code, so the result says what happened, see process.content_checks().
VERDICTS = {"wrong_content": "wrong content", "would_regrab": "would re-grab", "wrong_content_unconfirmed": "wrong content unconfirmed",
            "regrab_capped": "re-grab capped", "regrab_no_grab": "no grab record", "regrab_failed": "re-grab failed"}


def regrabbed(a, t):
    what = "the broken upgrade" if a.get("came") else "the file"
    if a.get("failed_before"):
        return f"The hook deleted {what} and re-monitored it. The grab was already marked failed with the rest of its download."
    if a["n"] == 1:
        return f'The hook deleted {what}, re-monitored it and marked the grab failed, so {a["name"]} searches again.'
    return (f'The hook deleted {a["n"]} {FAULTS[a["kind"]][2]} of this download, re-monitored them and marked the grab failed once, so '
            f'{a["name"]} searches again.')


# Per action code: the end of a fault's alert title, and the text. Every text ends with the restore, see restored().
ACTIONS = {
    "regrabbed": (", re-grabbed", regrabbed),
    "searched": (", re-grabbed", lambda a, t: f'The hook deleted the broken import and sent {a["name"]} a search for the item, because a manual '
                                              "import has no grab to mark failed."),
    "restored": (", old file restored", lambda a, t: "The hook deleted the broken import. It was a manual import, so no grab is marked failed "
                                                     f'and {a["name"]} does not search.'),
    "deleted": ("", lambda a, t: "The broken import is deleted. Its item was not monitored, so the hook sent no search."),
    "would_regrab": (", would re-grab", lambda a, t: f'A re-grab would delete the file and search again. REGRAB does not list {a["kind"]}, so '
                                                     "the file stays."),
    "unconfirmed": (", not confirmed", lambda a, t: "A second check did not find the same fault, so the file stays."),
    "capped": ("", lambda a, t: f'The cap of {a["cap"]} re-grabs a day is reached, so the file stays.'),
    "no_grab": ("", lambda a, t: f'{a["name"]} has no grab record for it, so the file stays.'),
    "failed": ("", lambda a, t: f'The {"restore" if a.get("manual") else "re-grab"} stopped at {a["step"]}: {a["error"]}'[:300]),
    "dry_run": ("", lambda a, t: "Dry run."),
    "no_policy": ("", lambda a, t: f'Skipped {a["file"]}. Fix the policy file.'),
}


def restored(a, text):
    """text with the restore of a re-grab around it, see regrab.restore_facts(): the job's own old file in front, then
    the other files of the download, then why the job's own old file stayed out."""
    came = a.get("came") or []
    if came:
        it = "it" if len(came) == 1 else "them"
        text = (f'The hook put back the old file{"s" if len(came) > 1 else ""} from {"its own copy" if a["own_copy"] else "the recycle bin"}: '
                f'{", ".join(came)}. '
                + (f'{a["name"]} links {it} again. ' if a["linked"]
                   else f'{a["name"]} did not link {it} within {regrab.RESTORE_WAIT} seconds, so rescan the item by hand. ') + text)
    if a.get("others"):
        text += f' The old file of {a["others"]} more broken file{"s" if a["others"] > 1 else ""} of this download came back too.'
    if a.get("stayed") and not came:
        text += f' The old file did not come back: {a["stayed"]}.'
    return text


def action_code(a):
    """The code that ends a fault's alert title: restored when a re-grab put the job's own old file back."""
    return "restored" if a["code"] == "regrabbed" and a.get("came") else a["code"]


def title(f):
    """(title, color) of the alert of finding f."""
    a = f.get("action")
    if f["kind"] in FAULT_TITLES and a:
        return FAULT_TITLES[f["kind"]] + ACTIONS[action_code(a)][0], "amber" if a["code"] == "unconfirmed" else "red"
    if f["kind"] == "repack" and f.get("note"):   # a conversion that a stopped run left
        return "Stopped conversion", "amber"
    return FINDINGS[f["kind"]][0], "amber"


def texts(f, t):
    """(text, action text or None) of finding f. A template that fails gives a line that says so, because an alert text
    must never cost the decision line or the post."""
    try:
        a = f.get("action")
        return FINDINGS[f["kind"]][1](f, t), restored(a, ACTIONS[a["code"]][1](a, t)) if a else None
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
KEPT_BACK = {"check": "SUBTITLES is check, so the file stays as it is", "keep_days": "KEEP_ORIGINALS_DAYS is 0, so the original could not be kept"}


def stays(x, t):
    """The end of the sentence of a track that does not match and stays in the file."""
    planned = t == "planned"
    flags = ("--apply would turn its default and forced flags off." if planned else "It loses its default and forced flags.") if x["flags_off"] \
        else "Its flags stay."
    if planned and x.get("hardlinked"):   # edit() and the remux both leave a hardlinked file as it is
        return block({"code": "hardlinked"})
    if planned and not x["gone"] and x.get("kept_back") == "keep_days":
        return ("The track would stay in the file, because a removal keeps the original, and KEEP_ORIGINALS_DAYS is 0. Set "
                f"KEEP_ORIGINALS_DAYS above 0 to remove it. {flags}")
    if planned and x["gone"] and x.get("block"):
        return "--apply would remove it." if x["block"]["code"] == "remux" else f'{block(x["block"])} The track would stay in the file. {flags}'
    why = x["result"] if x["gone"] else KEPT_BACK.get(x.get("kept_back"), "the file was not remuxed")
    return f"It stays in the file, because {why}. {flags}"


def where(x):
    return f'It moved to {x["kept"]}.' if x.get("kept") else f'It stays beside the file: {x.get("left") or "a dry run"}.'


def track(p):
    """A subtitle place as "track s2", a sidecar by its name."""
    return f"track {p}" if re.fullmatch(r"s\d+", p) else p


# Per sentence code of a subtitle alert, its text. subtitles.sub_findings() gives the codes and the facts.
SUB_LINES = {
    "removed": lambda x, t: f'Subtitle track {x["track"]} does not match the audio, {x["why"]}. {"The hook" if x["by"] == "hook" else "This run"} '
                            f'removed it, and the original file is kept at {x["kept"]}.',
    "stays": lambda x, t: f'Subtitle track {x["track"]} does not match the audio, {x["why"]}. {stays(x, t)}',
    "sidecar": lambda x, t: f'The sidecar {x["name"]} does not match the audio, {x["why"]}. {where(x)} A program such as Bazarr can download it again.',
    "converted_sidecar": lambda x, t: f'The sidecar {x["name"]} does not match the audio, {x["why"]}. The conversion left it out. {where(x)}',
    "converted_track": lambda x, t: f'Subtitle track {x["track"]} does not match the audio, {x["why"]}. The conversion left it out, and the '
                                    f'original file is kept at {x["kept"]}.',
    "sidecar_left": lambda x, t: f'The sidecar {x["name"]} needs new times, {x["why"]}, but it stays as it was: {x["left"]}.',
    "off": lambda x, t: f'Subtitle {track(x["track"])} ' + (f'disagrees with the reference {track(x["ref"]) if x["ref"][1:].isdigit() else "sidecar " + x["ref"]}'
                                                            if x.get("ref") else "is off the audio") + f': {x["why"]}. Its times stay.',
    "not_retimed": lambda x, t: f'Subtitle track {", ".join(x["tracks"])} needs new times'
                                + (f'. {block(x["block"])}' if t == "planned" else f', but {x["result"]}. The file stays as it was.'),
    "check_times": lambda x, t: f'Subtitle {x["track"]} needs new times: {x["why"]}. SUBTITLES is check, so its times stay.',
    "check_flash": lambda x, t: f'Subtitle {x["track"]} flashes its cues: its median cue shows {x["median"]:.2f} s. SUBTITLES is check, so its '
                                "ends stay.",
    "sweep": lambda x, t: "The sweep heard parts of the file off the fitted line: "
                          + ", ".join(f"{k} at {content.hms(at)} by {off:+.2f} s" for k, at, off in x["far"]) + ". The times stay as the check decided.",
}


def sub_line(x, t):
    return SUB_LINES[x["code"]](x, t)


def alert_line(f, t):
    """One alert of the decision log and the CLI: "kind: text action"."""
    text, act = texts(f, t)
    return f'{f["kind"]}: {text}' + (f" {act}" if act else "")


def alert_embed(rec, f, t):
    """The Discord embed of finding f of rec: the problem and what the hook did, one field with the title and the file,
    and the footer with the file's TMDB state, see logs.footer_b()."""
    (text, act), (head, color) = texts(f, t), title(f)
    return logs.embed(rec["app"], head, f"{text}\n{act}" if act else text, color, [(rec["label"], os.path.basename(rec["path"]))],
                      logs.footer_b(rec.get("tmdb")))


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
    logfmt: its one-line summary for syslog. Loki reads its keys, so they stay.
    embed: one Discord embed per finding.
    cli: the line of a backfill."""
    t = tense or tense_of(rec)
    if t not in TENSES:
        raise ValueError(f"no tense {t}")
    if target == "log":
        out = dict({k: v for k, v in rec.items() if k not in ("outcome", "took")}, schema=config.SCHEMA, version=config.VERSION,
                   policy=logs.policy_hash(), host=config.HOST, outcome=rec.get("outcome", "other"), took=rec.get("took"))
        if "findings" in rec:
            out["alerts"] = [alert_line(f, t) for f in rec["findings"]]
        return out
    if target == "logfmt":
        return config.mask(logfmt([("arr", rec.get("app")), ("source", rec.get("source")), ("outcome", rec.get("outcome", "other")),
                                   ("class", rec.get("class")), ("edits", len(rec.get("edits") or [])), ("reasons", ",".join(rec.get("reasons") or [])),
                                   ("alerts", ",".join(rec.get("alert_kinds") or [])), ("tmdb", rec.get("tmdb") or "not_asked"),
                                   ("label", rec.get("label")), ("id", rec.get("id"))]))
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
            + (" | ALERT " + "; ".join(alert_line(f, t) for f in rec["findings"]) if rec.get("findings") else ""))


def sub_time_report(rec, t):
    """The report of --sub-time on one file: its result, then one line per subtitle with its place or sidecar name,
    codec, language, role, method, verdict, offset and ratio, and action, then the rows of the sweep and the alerts."""
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
        elif p in (rm.get("remove") or []) or p in (rm.get("fixed") or []):
            act = ("removed" if p in (rm.get("remove") or []) else "retimed") if rm.get("done") else pending
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
        steps = cli.sweep_steps(rows)
        out += [f'    {content.hms(w["at"])} words {w["words"]} overlap {w["overlap"]:.0%} cues {w["cues"]} offset '
                + ("-" if w["offset"] is None else f'{w["offset"]:+.2f} s')
                + (f' ALERT {w["off"]:+.2f} s off the fitted line' if id(w) in steps else f' {w["off"]:+.2f} s off the fitted line, one window alone'
                   if cli.sweep_far(w) else "") for w in rows]
    if rec.get("full_read"):
        f = rec["full_read"]
        out.append(f'  whole-file read of {", ".join(f["tracks"])}: {f["took"]} s, {f["cpu"]} CPU s' + (f'; {f["why"]}' if f.get("why") else ""))
    if rec.get("unindexed"):
        out.append(f'  not read: {", ".join(rec["unindexed"]["tracks"])}, {rec["unindexed"]["why"]}')
    if rec.get("sweep_facts"):
        f = rec["sweep_facts"]
        cost = "sweep: words from the cache" if f.get("runs") and f.get("cached") == f["runs"] else f'sweep cost: {f["cpu"]} CPU s, {f["took"]} s'
        out.append(f'  {cost}' + "".join(f"; no words from {x}" for x in f["failed"]))
    out += [f"  ALERT {alert_line(f, t)}" for f in rec.get("findings") or []]
    return "\n".join(out)


def missed(rec):
    """Why a change that --sub-time --apply planned for the file of rec did not happen, or None: an error, a flag edit
    that failed or was not made, a subtitle remux that failed or was skipped, or a sidecar left as it was."""
    rm, hr = rec.get("subremux") or {}, rec.get("header_repair") or {}
    if rec.get("outcome") in ("error", "edit_failed", "verify_failed", "hardlinked", "read_only"):
        return rec["result"][:200]
    if hr.get("code") in ("header_repair_failed", "header_repair_skipped"):
        return hr["result"][:200]
    if (rm.get("fixed") or rm.get("ended") or rm.get("remove")) and not rm.get("done"):
        return rm.get("result", "the subtitle remux did not run")[:200]
    left = [e["name"] for e in rec.get("sidecars") or [] if e.get("result") == "left"]
    return f'the sidecar {", ".join(left)} stays as it was' if left else None
