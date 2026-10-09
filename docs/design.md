# Design

arr-media-guard runs on every Sonarr and Radarr import and upgrade, through a Custom Script
connection on a host or a Webhook connection in Docker. It makes the right audio track play first and sets the subtitle defaults to match. It checks that the
audio plays and the video decodes, and it re-grabs a file that is certainly broken. The same command
backfills the library and scans it for broken files. A second command hunts for releases with English subtitles.
Some releases of English films carry a foreign default audio track, and Plex plays the default.

This file says what each rule does and why it exists. The README covers the install, the env file
and the app connection. policy.md covers the policy file.

- A *job* is one file that waits in the background queue, for an import, a deep analysis or a recheck. AMG queues it,
  and a *worker* runs it.
- To *hear* a track is to let language detection name its spoken language.
- A *certain fault* proves that a file is broken and leads to a re-grab. A *doubt* only alerts.
- A *re-grab* deletes a broken file through the app, monitors its items again and marks the grab
  failed. The app then blocklists the release and searches again.

## What it changes

The hook changes the default flags of the audio and subtitle tracks, with `mkvpropedit`. It clears
the forced flag of an English subtitle that holds the full dialogue, and it fixes language tags, see
"Language tags". It converts a file that is not Matroska into `<base>.mkv`, the only rename, see
"Conversion". It remuxes a broken Matroska header, see "Header repair". It skips a hardlinked file,
because an edit would also change the download client's copy.

The rules are in `arr_media_guard/decide.py`. The data they read is in the policy file, so most new cases need
only a policy change. A decision has four steps.

**1. Classify each track.** Every audio and subtitle track gets a language, a confidence and a role.

- **Language.** The tag is cross-checked with a language the track title names. They agree (1.0),
  the title wins over the tag (0.8), or the tag stands alone (0.6). A track titled "French" and
  tagged `eng` is French. An audio title drops a word before Dub, Score or Sub, so "UK Dub /
  Japanese Score" names no language. A heard language that agrees raises the track to 1.0. One that
  disagrees replaces the language at 0.9.
- **Audio role.** Main, commentary ("Commentary", "cmt", "Comm") or description ("AD", "DVS"). Only
  a main track plays first.
- **Subtitle role.** Commentary, dub transcript ("English (dub)", "Dubtitle"), forced, SDH or full.
  A "forced" or signs title (signs, songs, foreign parts, alien, titles only) makes a track forced.
  So does a forced flag, or a density under `sparse_events` (1.5 events a minute). "SDH", "CC" or
  "full" in the title overrides the signs words.
- **Density.** The events a minute come from mkvmerge's statistics tags. They count only when
  mkvmerge wrote the file and it runs `density_min_minutes` (15) or more. A PGS count is halved,
  because PGS stores two frames per subtitle. A forced flag counts only under `forced_flag_events`
  (4). A forced flag on a denser track is a conflict (`dense_forced_flag`). So is a full or SDH
  track under 1.5 events a minute (`sparse_full_title`).

**2. Pick the audio, then the subtitles.** The item is an English original, a foreign original or a
foreign kids title. The policy's `audio` lists the languages to try per class, the original and then
English. A kids title tries English first, so a foreign kids film plays its English dub. `kids`
lists the genres, quality profiles and studios of a kids title. Animation alone does not count, so
anime keeps its original audio.

- A default audio track in the right language stays, even when a later one has more channels.
- The audio flags change when another track must play first, or when the file has no default audio
  flag or several.
- A file with no English or original-language main track is left alone and alerts. Untagged audio
  never alerts.
- Under English audio, only the roles in `subtitles.english` (forced) stay on. When that turns an
  English track off and no forced English track stays on, the forced English track with the fewest
  events turns on.
- Under other audio, `subtitles.foreign` ranks the English subtitles in the order full, SDH,
  forced, dub transcript. The best one becomes the only default.

**The forced-flag clear.** Plex shows a forced track in the audio's language even without the
default flag. So a full-dialogue English track flagged forced shows on its own under English audio.
`forced_clear` clears the flag of an English full or SDH track at `events` (10) or more events a
minute. Its title must not say forced or name signs. With `english_only_audio`, English must be the
only main audio language. Beside other full or SDH subtitles, the track must reach
`reference_ratio` (0.8) of their median events. An English original also needs TMDB to list English
as its only spoken language, because an English show with foreign scenes needs its subtitle for
them. An unknown TMDB keeps the flag.

**3. Abstain on conflicting signals.** The plan is then empty, and the log says `undecided`.

- `original_missing_bare_tag`. No track is in the original language, and the English audio that
  would play is a bare `eng` tag below `min_confidence` (0.7). A title that names English, or
  "English" or "Dubbed" in the release name, makes it certain.
- `dense_forced_flag_english_only`. English is the only audio language, and the plan would turn off
  a default English subtitle whose dense forced flag the clear kept. The flag is then the only sign
  of what the release meant to show.
- `sparse_full_title`. Other audio plays, the best English subtitle is forced, and another English
  track has a `sparse_full_title` conflict.
- `untagged_may_be_original`. A foreign original has no tagged original track, and an untagged main
  track names no language. That track may be the original. The hook hears such a file, see "Audio
  language detection".

**4. Check the invariants.** A plan with edits is dropped and reported when the state it leaves
breaks a rule. The first `audio` target the file has must play. Under English audio, only an English
subtitle in a `subtitles.english` role may be default. Exactly one audio track is default. No
commentary or description track is default. Under other audio, the only English subtitle that was on
stays on. After an edit the hook probes the file again and checks every flag.

## Language tags

A Matroska track has a legacy ISO 639-2 tag (`eng`) and a newer BCP 47 tag (`en`, `en-US`). The BCP
47 tag wins when both are there. `mkvpropedit --set language=<tag>` writes both, in the same call as
the flag edits.

A tag changes only when two signals agree and one of them is the heard or the read language. The
signals are the legacy tag, a BCP 47 tag that names another language, the heard language of an audio
track, the read language of a subtitle, and the title's language, see "Subtitle text".
A main audio track also has the item's original language. TMDB's spoken languages count only for an
`und` track, because TMDB lists English as spoken for many foreign films. The language with the most
signals wins when it has two or more and no other language has as many.

- The hook hears a main audio track that is `und`, whose two tags disagree, or whose title names
  another language. Commentary and description tracks are never heard.
- A Spanish track tagged `eng` in a Spanish film gets `es`, because the heard and the original
  language beat the tag. A title a muxer copied onto an English dub changes nothing, because the
  heard English keeps the tag.
- A subtitle changes its language only with its text and one more signal, such as its title. An
  `und` subtitle takes its read language alone, because its tag makes no claim. A title or a BCP 47
  tag that names a language is a claim, and the tag then stays.
- A new language keeps a BCP 47 tag that names it or a language inside it, so `yue` and `cmn-Hant`
  stay. A kept language keeps its BCP 47 language, script and region. The script stays, because Plex
  shows Traditional and Simplified Chinese both as 中文 without it.
- `mul` and `zxx` stay. An undecided or dropped plan gets no tag edit.

The undo restores both tags, with a second `language-ietf` edit or a `--delete language-ietf` where
needed. Plex shows the BCP 47 tag, else the legacy tag, and it shows `und` audio as English. Hearing
is slow, so a first backfill over many `und` tracks can take hours. The cache makes later runs fast.

## Original language flag

Matroska's `FlagOriginal` is set when a track is in the content's original language. `decide.retag()` plans it with
the language tags, as the mkvpropedit property `flag-original` (`decide.ORIGINAL_FLAG`). The content's original
language is TMDB's, see `decide.content_language()`. When the app lists another original language for the item, or
TMDB gives none or one with no 639-2 code, no flag changes.

- Only the roles of `decide.ORIGINAL_ROLES` change: main audio, and full, SDH, forced and dub subtitles. A commentary
  or an audio description is no part of the content's own speech, so its flag stays.
- AMG must be sure of the track's language, by the bar of `min_confidence` (owner 2026-10-06, "hear before
  flagging"). The track is tagged after this run, not `und`, `mul` or `zxx`, so an `und` track that the tag rules tag
  gets its flag in the same run. A main audio track needs its heard language to name its
  language after this run. With nothing heard, its tag and title must agree with no other signal against them, which
  is `AGREE`. A subtitle needs the language read in its text to name its language. A tag alone never decides.
- `decide.flag_checks()` names the tracks whose flag their tag would change. `process.languages()` adds those
  subtitles to its text read. `process.flag_hearing()` runs after the subtitle check. A subtitle whose words matched
  an audio track there (verdict `match`) proves that the track speaks its language, so that track needs no hearing,
  and the decision line keeps it in `flag_matched` (owner 2026-10-06). A mismatch proves nothing. `checks.hear()`
  hears each other track, up to three 30-second samples cached by the file, and the decision line keeps its answers in
  `flag_heard`. What is matched, heard or read for the flag alone goes to `retag()` as `checked`, so it changes no tag
  and no default flag. A missing install, a failed run or no time left gives no answer, so no flag changes, and a
  note names the track.
- A track tagged by its BCP 47 tag alone reads as `eng` beside it. The tag rules hear it, and that heard language
  decides it.
- A track in the original language gets 1. A 1 on a track in another language goes to 0. A right flag, and a missing
  flag that would be 0, stay. An edit is `[selector, new, old, ORIGINAL_FLAG]`, with old `None` for a missing flag,
  whose undo is `--delete flag-original`. `edit()` drops a no-op, and `unapplied()` verifies it, a missing flag
  reading as 0. Its rules are `original flag set` and `original flag cleared`, so the audit groups them as other
  flag edits.
- An undecided or dropped plan gets none, as for the tags. The deep analysis, a recheck and `--sub-time` on a file
  no app lists keep the flags, see `process.act()`. No recheck entry exists, so old files change only in a backfill.
  `cli.selected()` leaves out a file with one audio track, no subtitles and no tag to fix, so a backfill never sets
  its flag.
- An edit of this flag alone queues no Plex analyze, see `process.changed()`. Plex does not read the flag, and most
  imports get one. Any other edit queues the analyze as before.
- The decision line's `tracks` shows each track's flag as `orig`, 1, 0 or `null`, as the run found it.

## Plex

After an edit, Plex re-analyzes the one item that holds the file (`PUT
/library/metadata/<ratingKey>/analyze`), so it reads the new flags. The hook never scans a whole
section. With `PLEX_URL` empty, the hook and the backfill make no Plex call at all, and
`--plex-flush` exits with a message.

Plex has no lookup by external id. So the hook searches by title in the section that holds the
path, and the app's ids pick the item. For a show it reads every episode, because the numbering can
differ between Sonarr and Plex. `PLEX_PATH_MAP` maps the local path to the path Plex lists. A new import
is often not in Plex yet. The worker looks again 15, 45, 105, 225, 405 and 600 seconds after the
edit. After that the app's own Plex connection adds the item, with its flags already fixed.

**The idle-section gate.** Plex can crash when an analyze reaches an item while a scan of its
section replaces the item's media. So the section must be idle on two checks of `GET /activities`,
`PLEX_QUIET` apart. A scan that names no section yet, and a single-item refresh, count for every
section. A failed check counts as busy. A busy section defers the analyze by `PLEX_BUSY_WAIT`. After
`PLEX_BUSY_CAP` the worker gives up, because Plex's own scan reads the changed file anyway.

**The burst.** A backfill edits files in place, and the app does not import them. So an edit starts a scan only
when Plex watches the library folders ("Scan my library automatically"). When Plex does not, a backfill confirms the
section idle once, and each analyze after that needs one fresh idle check right before its request. The idle checks
form a row. A check at most `PLEX_QUIET` after the last one continues the row, and the row must span `PLEX_QUIET`.
A busy check, a failed check or a longer gap ends the row, and the next analyze waits for two checks again. The
backfill reads the setting before each analyze that would use a row. When it is on, missing or does not read, the
row ends for that file.

Each request still follows a fresh check that finds no scan in its section. The checks come at least as close
together as the two checks did. Plex's work after an analyze (loudness, credits, chapter thumbnails, ad detection)
is no scan, so it never ends a row. Each analyze leaves the same work under both rules, and `PLEX_PACE` still
separates two requests. A scan that starts after the last check can meet that work under either rule. A row belongs
to one backfill process. The worker, another backfill and `--plex-flush` never use it.

The worker keeps two checks for each item. An import makes the app ask Plex to scan the item's folder, and one check
can come before that scan starts. The worker runs the checks of its waiting items side by side, so a season pack
does not wait `PLEX_QUIET` for each file.

**A folder scan.** A renamed file is not in Plex until Plex scans its folder. So a conversion or a
restore under another name queues a partial scan of its one folder. Plex's work after an analyze
shows no activity, so the scan also waits `PLEX_SCAN_AFTER` after the last analyze in its section. Each analyze line
of the decision log also stores its time in the state store, so the worker and `--plex-flush` wait for a backfill's
analyzes too. A backfill writes the decision line of an analyzed file before its `PLEX_PACE` pause.
An analyze waits while a folder scan of its section is pending.

## Alerts

Every alert is a Discord embed to `DISCORD_WEBHOOK`, one per file and problem. The username names
the app and `INSTANCE`. The embed says what is wrong and what the hook did, in a viewer's words, and
gives no advice. The footer names the host. On a language or content alert it also names a TMDB failure,
because TMDB's language check then did not run. Red is broken audio, corrupt video, wrong content or a damaged source. Amber is every
other alert. Mentions are off, and the secrets are masked. A marker in
the state store stops a repeat, and an upgrade to a file of another size alerts again.

**What posts.** `report.posts()` is the one gate, and `logs.alert_findings()` asks it for every finding. A problem the
hook left unresolved posts. That is a failed fix, a fix that a setting turns off, a fix that cannot run, or a doubt. A
problem it fixed goes to the decision log only, see `report.fixed()`. That is a fault with the action `regrabbed`, `restored`
or `searched`, and a subtitle sentence `removed` or `converted_track`, or a `sidecar` or `converted_sidecar` that moved.
A restore that put back no old file of its own failed, and so did one whose old files the app did not pick up. Both
post. A subtitle finding posts when one of its sentences does. One cause posts once. When the hook deleted the file, its
other findings are log only. A `content` finding names each signal that scored, and lists their kinds in `scored`. A
`language` or `runtime` finding beside it is log only when `scored` names that signal. The episode signal scores no
points, so the `episode` finding posts. A `duration` finding is log only when the header repair fixed the file, or
when a `header`, `cut` or `subtitle` finding of the file posts its cause. It posts when no repair ran, as with
`HEADER_REPAIR` off or a repair that another fault blocked. The decision line keeps every finding, and its
`alert_result` says `log only` for each one that did not post. An alert whose text fails still posts, with its title
and its file, and the sentence `report.UNTOLD` in place of the error. Its line in `alerts` keeps the error.

**Check and step.** `report.stage_fields()` gives each issue alert two inline fields under its text and above the item
field. Check names the check that found the problem, by the source of the record, see `report.CHECKS`. The sources are
`hook`, `deep_analysis` and `recheck`. A conversion that a stopped run left has `worker` when the worker finds it as it
starts, and `backfill` when a `--convert` run finds it, see `convert.pending_recover()`. Stopped at names the step where
the fix stopped, one of `report.FIX_STEPS`, see `report.stopped_at()`. A finding with an action takes its step from
`ACTION_STEPS`. A subtitle finding takes it from the sentences that did not fix their subtitle, see `LINE_STEPS`. Any
other finding takes it from `FINDING_STEPS`. Each step there is a name, `None`, or a function of the facts. `None`
means the program tried no fix: a doubt, a check that only reports, a fix that a setting or a limit turned off, and a
dry run. A sidecar left in place names no step when its reason starts with a setting, see `report.SETTING_LEFT`. A
live-captioned track whose hearing stopped part way carries `hearing_stopped`, see `subtitles.sub_findings()`. A
conversion that finished, whose extras wait for the app to take the new file, stopped at Replacing the file. Each
subtitle counts at the furthest step its sentences name, see `report.line_keys()`. Subtitles at different steps show
one step a line, in the order of `FIX_STEPS`. A field with no value is left out, so an alert with no fix tried shows
Check alone. A held alert keeps the fields the import gave it. A change post has neither field, see
`report.fix_post()`. A test fails when a finding kind, action code or sentence code has no step.

**Plain words.** A subtitle remux that failed reads "rewriting the file failed" in the alert, see `report.remux_why()`,
and ends with "The original file was not changed." A failed header repair and a failed garbled-text repair end with the
same sentence. The decision line keeps its error in `subremux.result`. A skip keeps its own reason, such as low space or
a second hard link. A sidecar whose new times do not fit its text says so in `left`, and keeps the error in `error`, see
`subtitles.sidecar_fix()`. A fix with a ratio names the offset at the start and at the end of the file, see
`report.at_end()`. The end comes from the fix and the file's length. A `check_times` sentence, and an `off` sentence
with `would`, carry that length as `duration`. A change post reads it from the record's `file_duration`. An offset under
0.05 seconds reads "in sync", see `report.about()`.

**Every change.** With `DISCORD_POSTS=all`, `logs.alert_findings()` also posts each change of the run, see
`report.render()` target `changes`. A finding of `report.fixes()` that the gate logs only keeps its alert's words and
color. A change that no finding words posts in green, see `report.changes()`. That is a conversion to MKV, a file
repair, retimed subtitles or a track edit. A track edit says each kind it made: default flags, forced flags and
language tags. One fix posts once. A change that a posted alert already says is left out, see `report.said()`: lines a
live caption alert moved, lines an alert of moved parts counts, flags a `stays` or `sublang` alert turned off, and a
conversion that a subtitle sentence names. After a re-grab deleted the file, only the re-grab posts, as in issues mode. A change whose text fails posts
nothing. Its line in `change_result` starts with `no text:`, and the other changes still post. These posts skip the
marker, because a second run finds nothing left to change. `change_result` in the decision line keeps what each post
gave.

**One alert per problem.** With `SUBTITLES=deep`, an import whose file gets the deep analysis holds its `submatch` and
`subtiming` alerts, see `logs.DEEP_KINDS` and `process.alerts()`. The deep analysis checks those subtitles again, so it
alone posts what is still wrong. A held alert never posts at the import, and its `alert_result` says `held for the deep
analysis`. Its kind, size and embed go into the deep analysis job as `held`, with the subtitles it names in `keys`, see
`runner.queue_deep_analysis()` and `report.named()`. A track goes by its place after the import's remux. The job ends
with `runner.held_after()`, which decides each held alert. The alert goes unposted when the file is gone for good, see
`runner.gone_for_good()`. That is a file with another inode at its path, or a file the app says it no longer has for the
item. The app says so with a 404, another movie or series, or other episodes, see `runner.moved()`. A file the app lists
at its old missing path or at a path this container does not see is not gone, so its held alerts post. A person's
`--sub-check --apply` or `--sub-time --apply` remux gives the file a new inode too, so it counts as replaced. That run
checked the subtitles itself. The alert also goes unposted when the job ran to its alerts and judged each subtitle the
alert names, see `runner.judged()`. A verdict of `match`, `fit` or `mismatch` judges. The `unknown` of a failed hearing
does not, and with no language model there is no verdict. A held `subtiming` alert also needs the times of each subtitle
judged again, see `runner.times_judged()`. The timing judge gave each one an outcome other than unknown, see "Timing
outcome", or the subtitle does not match the audio. That outcome posts itself when lines are still off. A word check that matched
the words but anchored too few cues to time them judged no times. When the whole-file timing judged nothing either,
the alert posts. A job whose remux or sidecar
rewrite retimed, moved or removed those subtitles before an error counts too, see `runner.remuxed()`. Longer ends and
a repaired text do not. Every other held alert posts through `logs.post_held()`, because nothing judged its subtitles
again. That is an error, a failed hearing, no language model, `SUBTITLES=off`, a file the app moved during the run, or a
third crash in `runner.requeue()`. When the import cannot queue the job, its held alerts post at once. With
`DISCORD_POSTS=all`, a held finding keeps the change posts of what it says itself as `changes`, see
`report.held_posts()`. That is a fix of its logged lines, such as a removed track, a flag it says was turned off, and
the conversion it names. `report.said()` leaves them out of the import's change posts. `logs.held_changes()` posts them
when the held alert goes unposted. It skips a flag edit that a posted alert of the deep analysis says already, as when
the deep analysis finds the same mismatch. A held alert that posts names the change itself. So each change posts once.
Every other kind posts at the import.

**Track numbers.** Every post of a run names a subtitle by its place in the file after the run, see `report.number()`.
A track the subtitle remux took out is "track 2 of the original file". The decision log and the CLI keep the places
the check saw.

**Track names.** A name says the language and the role, as a player lists the track: "the English SDH subtitles
(track 3)", "the English commentary audio (track 4)", see `report.track_name()`. The role comes from the decision log
entry of the track, see `logs.track_log()`. A full subtitle and a main audio track name no role. The track's own title
follows the number in quotes when it adds something, see `report.shown_title()`. A title of the track's language, its
codec, its role, its flags, numbers and lone letters only is left out, in any case or brackets. A commentary title never
shows, because it may name people at length. Control, markdown and link characters go. A bare URL goes too, because
Discord makes it a link and a release can plant one. A title over 40 characters is cut at a word with "…". Two tracks of
one language share one name only when neither has a role or a title to name.

**Format.** A template marks the names to look for and the key fact of an alert, see `report.bold()`. The embed bolds
them, and the decision log, the CLI and Loki show plain text. `logs.embed()` escapes each Discord markdown character
(`\`, `*`, `_`, `~`, a backquote and `|`), so a name never breaks the format. A text of more than two sentences gets one
sentence a line.

**Link.** The embed's item field shows the item's name as a link to its page in the app, above the file name. Discord
shows no link in a field name, so the field name is then blank. The page is `/series/<titleSlug>` in Sonarr and
`/movie/<titleSlug>` in Radarr, see `apps.App.page()`. The job takes the slug from the series or movie record it reads
anyway, and the decision line keeps it in `ids.slug`. The base is `<KEY>_LINK` alone, because behind a reverse proxy
or in Docker the address a browser opens differs from `<KEY>_URL`. An empty `<KEY>_LINK` gives no link. The link keeps
only the scheme, host, port and path of the base, so it never holds a user, a password or a query. A record with no
slug, as an error, gets the field of before: the name, then the file. So does a name whose brackets do not pair,
because Discord ends a link at a lone bracket. `report.link()` marks the link, and only an embed shows it. The subtitle
hunter links the movie in the Title field of its posts.

| Kind | Fires when |
| --- | --- |
| `language` | No main audio track is English, the original language or a language TMDB lists. |
| `runtime` | The trusted duration is far off the listed runtime, see "Metadata checks". |
| `duration` | The header duration disagrees with the size or the other duration sources. The size check trusts BPS tags only from mkvmerge, because ffmpeg copies stale tags. Else it allows 50 Mbit/s up to 1080p and 150 above. |
| `episode` | The release names another episode than the one Sonarr imported it as. The alert leads with the imported episode and its title. |
| `content` | The evidence adds up to a re-grab. The title ends "re-grab is off" when `REGRAB` does not list `content`. |
| `audio`, `video` | See "Broken audio" and "Corrupt video". |
| `edit` | mkvpropedit failed, or the second probe shows the old flags. |
| `damage` | The conversion of an import shows a damaged source, see "Damaged source". |
| `repack`, `header` | A conversion or a header repair failed, and the original stays. A conversion a stopped run left alerts as `repack` too. |
| `subtitle`, `cut` | A subtitle runs far past the end. It is not SubRip, or the file may be cut. |
| `sublang` | A subtitle's text reads as another language than its tag, and nothing else backs a new tag. |
| `submatch`, `subtiming` | A subtitle does not match the audio, or its times are off and stay, see "Subtitle match". |
| `policy` | The policy file is missing or does not load. |

A fault that the second check did not find again is amber, "not confirmed". A rejected TMDB key
posts one embed a day. It names the key that failed, which is `TMDB_TOKEN`, Radarr's key in its DLL, or the built-in
copy of Radarr's key. A 429 from Discord waits `retry_after` and tries again, up to `logs.POST_TRIES` tries in all,
because several job processes may post at once, as for a season pack. A `retry_after` over `logs.POST_WAIT` seconds,
or a 429 on the last try, gives up. The later posts of that process then skip until the time Discord named, and each
result says so. A state store that the
worker moved aside posts one embed a day too, see "State".

**One output model.** The checks and the steps keep codes and facts. A finding is `{"kind": <kind>, ...facts}`, and its
kind is the alert kind above. A finding the hook acted on holds the action, `{"code": <code>, ...facts}`. `report.py`
words them, with one template per finding kind, per action code and per sentence of a subtitle alert.
`report.render(record, target, tense)` gives each output of one decision:

| Target | Output |
| --- | --- |
| `log` | the JSON decision line, with the text of each finding in `alerts` |
| `logfmt` | the syslog summary, see "Logs" |
| `embed` | one Discord embed per finding |
| `cli` | the line a backfill prints for the file |

The tense is `planned` for a dry run and `done` for an apply, so a dry run says what `--apply` would do. The decision
line of a dry run says the same as its CLI line.

The action codes are `regrabbed`, `restored` (a manual import's old file came back), `searched`, `deleted`,
`would_regrab`, `unconfirmed`, `capped`, `no_grab`, `failed`, `dry_run` and `no_policy`. The title of a re-grab says
"old file restored" when the job's own old file came back. Each sentence of a `submatch` or `subtiming` finding has a
code too: `removed`, `stays`, `sidecar`, `converted_sidecar`, `converted_track`, `sidecar_left`, `off`, `not_retimed`,
`check_times`, `check_flash` and `live`. The codes are stable, and the words may change.

## Broken audio

The worker checks the audio track that plays. ffmpeg decodes three 20-second samples at 10, 50 and
85 percent, with `volumedetect`. A file is certainly broken only in these cases:

- mkvmerge and ffprobe find no audio track, or both fail to read the file and it has no known
  container signature.
- All three samples are digital silence, or all three log decode errors and lose audio.
- The late sample decodes nothing, and ffmpeg logs `File ended prematurely` or `partial file`.
- All three samples decode nothing, the audio packets end before the first sample, and the video
  runs on with no gap over 10 seconds.
- An AC-3, E-AC-3 or DTS track holds under 90 percent of the video, and a sample in its longest
  hole of 30 seconds or more decodes nothing.
- A file under 10 minutes decodes under 90 percent of its track, with decode errors.

A sample that did not run, one or two bad samples, and a late sample that decodes nothing without a
cut are doubts. Before a delete, the worker checks the file again from the start and deletes only on
the same certain fault.

The silence line is -80 dB. Digital silence reads about -91 dB, and quiet real audio reads far
louder. Clean files often log a decode error at the seek point, so a sample fails only when it also
decodes under 95 percent of its audio. The expected length comes from ffmpeg's own output stream
line. mkvmerge cannot read ASF or WMV, so there ffprobe alone finds the audio and every fault is a
doubt.

**Where the samples go.** They cover the later of the audio end and the video end. The ends come
from the DURATION tags mkvmerge wrote for this file, else the container, else ffprobe. A tag another
tool copied counts for nothing. One SubRip event can stretch the Segment duration to hours, and a
sample past the real end decodes nothing.

**More reads.** When the three samples prove nothing, `audio_more()` reads more of the file:

- `full` decodes the whole audio track of a file under 10 minutes.
- `packets` reads every packet with ffprobe and decodes nothing. It finds where the audio, the video
  and each subtitle end, and the longest hole in the audio.
- `hole` takes one more sample inside that hole, for a constant-rate audio codec.

A gap of over 10 seconds in the video gives no verdict. One stray packet or a timestamp wrap makes
such a gap in a good file. A silent late sample counts as end credits when every subtitle ends before it and a text
subtitle has 4 or more events a minute. A whole-file read may use at most 80 of the job's 300 seconds,
because the video checks keep 220. The hook skips a file too large for that at `READ_RATE`, and a scan reads it later with no time limit.

**The re-grab.** A certain fault skips the flag edit. A download is one unit. When the app has a
grab record, the worker samples every file of the download that is still in the library. It deletes
each broken one, monitors the items again, and then marks the grab failed once. The app searches at
once and would reject the replacement while a broken file is on disk, so the order matters. A delete
goes to the app's recycle bin, and an upgrade first gets its old file back, see "Restore after a bad
upgrade". The state store keeps each download for 7 days. `REGRAB_CAP` (30) limits the re-grabs per instance
a day, and a season pack counts once. Broken audio, wrong content, a damaged source and corrupt video
share that one count, and 0 turns every re-grab off. The count and its check run in one transaction. Past the cap, or with no grab
record, the worker only alerts. `REGRAB` lists the kinds that re-grab, `audio,video` by default. A
kind it does not list still gets the grab record, the cap and the second check. The worker then logs
`would_regrab` for wrong content, posts "re-grab is off" and deletes nothing.

## Corrupt video

The worker checks the video after the audio, unless the audio is certainly broken. The check only
reads. Each stage runs only when the earlier ones found nothing certain.

| Stage | What it reads | Certain | Doubt |
| --- | --- | --- | --- |
| `header` | the Matroska start and Cues, see "Header repair" | 64 KiB or more missing at the end | less missing, or more after the hook's own failed edit |
| `zeros` | 256 reads of 64 KiB over the Clusters. A zero run counts from 256 KiB. | runs at 2 or more offsets | a run at 1 offset |
| `windows` | 5 seconds of video at 10, 50 and 85 percent of the video end | 2 or more bad windows | 1 bad, empty, stopped or failed window |

A window is bad when a decoder error comes after the first frame, or a demuxer error comes anywhere.
It is bad when two frames sit over 1 second apart, or the first frame is over 30 seconds late. A
window with no frame and an error is bad too. A decoder line before the first frame belongs to the
seek. The h264 `SEI type ... truncated` and mpeg4 `low_delay flag set incorrectly` lines never
count, because clean files log them too.

Clean encoders pad easy frames with short zero runs, so only a run of 256 KiB counts. The zero probe
skips the Attachments and anything past the Segment end, where fonts and old copies can hold zeros.
Three windows rarely meet a small hole, so they are for damage spread through the file. An encrypted
track has no decoder, and neither does a codec ffmpeg does not know. So such windows are certain
only with evidence of encryption, such as an `encv` FourCC, a ContentEncryption element or an MP4
`tenc` box.

A Segment duration can run far past the video. So the windows sit on the video end read from the
last Clusters, else mkvmerge's DURATION tag, else ffprobe. A file that is not Matroska gets a
doubt when its format duration runs over 60 seconds past its video end (`AV_APART`). A window stops
at 60 seconds or 512 MiB read, because a seek with no index reads from the file start. A time-cap
stop is a doubt, and a read-cap stop with no error is only logged. In the hook each stage needs time
left and keeps `VIDEO_RESERVE` for the edit. An exception gives `video_check_error`, and the edit
goes ahead.

A certain fault re-grabs like broken audio. The second check moves its zero reads by half a step and
its windows to 30, 70 and 95 percent, and it must find the same fault class. So a local fault may
stay an amber "Broken video, not confirmed". It counts against the same `REGRAB_CAP` as broken audio.
Decode each certain file and each doubt of a `--check-video` scan in
full before you act on it.

## Restore after a bad upgrade

A plain re-grab of a broken upgrade leaves the item with no file. So the re-grab first puts the old
file back from the app's recycle bin, and the app still searches for a better one. A broken manual
import that replaced a file is undone the same way. `RESTORE=false` turns both off.

On an upgrade, Sonarr 4 and Radarr 6 pass the replaced paths in `<app>_deletedpaths` and their bin
paths in `<app>_deletedrecyclebinpaths`, so the hook never searches the bin. An old file comes back
only when all of these hold:

- The bin still holds it, and no other file took the old path.
- For Sonarr, every episode of the old file is an episode of the broken file, by the parse API. An
  old s01e01-e02 file never comes back for a broken s01e01.
- The bin is on the file system of the old path, so the move is a rename. Keep the recycle bin on
  the media's file system, or no old file comes back.
- The bin exists where the hook runs. When `KEEP_REPLACED` keeps no copies, `--selftest` and Test
  warn when it does not, as in a container that does not mount it.
- The old file passes the import's audio and video checks. A certain fault, an audio sample that
  did not run, or a stopped video check keeps it out. Other doubts do not, because it played before.
- The bin file still has the inode, size and mtime the plan checked, because the app may give a
  freed bin name to the broken file itself.

The steps run with SIGTERM blocked. A `restoring` line goes to the log, so a killed job leaves a
record. The app deletes each broken file into its bin first, because it deletes the file at the
item's path. Each old file goes back by a rename that never overwrites. The extras follow. For each
name, the copy nearest the video's mtime within `EXTRA_SLACK` is this upgrade's. The items are
monitored again, the app rescans them, and the hook reads the record back. Last, the grab is marked
failed once, and the app searches against the old file.

A manual import has no grab to mark failed, so the app does not search. With no old file, the import
stays and alerts. When no old file came back after the delete, the hook searches for the items that
were monitored. An API search grabs an unmonitored item too, so with none monitored nothing is
searched.

**The hook's own copy.** Without a recycle bin, with a bin on another file system, or with a bin
this host does not see, no old file comes back. `KEEP_REPLACED=true` covers these cases. The app
deletes the old file before the import calls the hook, so the hook keeps it at the Grab event, which
the app sends when it picks a release. For each item of the grab that has a file, the hook
hard-links the video and its extras into `<mount>/.<NAME>-recycle/<UTC time>/<path from the mount>`,
the layout of the kept originals. `replaced_root()` picks the folder. A Sonarr grab can cover a
season, and a file of several episodes is linked once. An anime batch can cross a season, so for an
anime series the hook matches the absolute episode numbers. A link needs no space at the grab, and
the file keeps its space until the prune. A copy could lose the race to the import, so where the
file system refuses a link, the hook keeps nothing of that file and logs one line. A file the app
lists and that is not on disk gets a line too. An error after the first link removes the links of
the grab, so no link stays without its record. The Grab answer is always ok, and an error only logs.
The hook keeps the links in `.<NAME>-recycle`, in the folder that "Kept originals" describes.

The state store holds one record per link. A record names the old path, the kept
path, the grab's download id, the time and the inode. The import that replaces the old path claims
the records of its own grab, or of a grab with no download id. A restore takes the copy only when
the app's bin has none it can use, and only for the import that claimed it. A usable bin copy wins.
A copy within an hour of `KEEP_ORIGINALS_DAYS` stays out, because the prune may remove it during the
restore. The copy then gets the same checks as a bin copy, and its extras come back with it.

A copy goes stale when the old path changes after the grab. A repair, a tail cut, a subtitle fix and
a conversion under the same name rename a new file over the path. A restore renames an old file back
over it, and another import replaces it. Each marks the records of the path stale, so a restore
never brings back an older version. A later import also makes a claimed record stale. A change by
the hook leaves a claimed record, because it holds the file its import replaced. The plan names why
the old file stayed out. A flag edit changes the file in place, so the copy shares it and stays
valid. The hook's own link does not count as a download client's hard link, so it never blocks an
edit or a repair. A record matches the file by its inode, so a file the app renamed after the grab
still finds its link. A record stays while its link exists.

`KEEP_ORIGINALS_DAYS` caps the copies. The nightly audit removes the time folders older than that,
in the folders of the app's root folders and in each folder a record names. The record covers a
mount below a root folder, such as a dataset per show. The worker also prunes the folders the
records name once a day, so a host with no nightly audit prunes too. A grab never prunes, so the app
never waits for it. With `0`, the hook keeps nothing, and a prune removes every grab link.
`--selftest` and Test warn when the app has no saved connection to the hook, when that connection
does not send Grab, and when the hook cannot hard-link a file on a mount. They probe each mount with a small temp file that they
remove. With the copies working, the bin gives no warning. The hook's connection is a Custom Script
with this script's path, or a Webhook to `/radarr` or `/sonarr` with the user `WEBHOOK_USER`. The
listener's start check and the nightly audit turn On Grab on in it when it sends On File Import or
On File Upgrade. Each change gets a line in the decision log and one on stdout, the container log
in Docker. Test and `--selftest` never write, because the app saves the connection after its Test.

Two bind mounts of one file system share `st_dev`, but a rename between them fails. So the restore
and the bin warning compare the mount tops too. A bin under another mount top counts as another
volume.

## How it runs

The app waits for the hook to exit. The hook answers the Test event with the checks of
`runner.app_check()`: the policy loaded, the app's API answers with its key, and the script sees each
root folder. A failed check exits 1, so the Test fails, as the listener's Test does. A recycle bin
warning only prints. On an import or an upgrade it puts one job into the queue of the state store, forks
a worker if none runs, and exits 0.
`worker.lock` keeps one worker. The worker runs the queue oldest first and checks it once more after
it drops its lock, so no job is lost. A job older than a day is dropped.

`HOOK_WORKERS` (2) sets how many jobs run at a time. Set it to about the cores the host can spare.
With more than 1, the worker claims each job by one change in the state store, forks one process
per job, and sends every Plex analyze itself, so the idle-section gate holds. A season pack of 200
episodes runs at most `HOOK_WORKERS` processes at a time. A job process that crashes writes one decision line with
outcome `error`, the exception and a short trace, because its stderr is often `/dev/null`. A job whose processes crash
`CRASH_TRIES` times in one worker run is not claimed again in that run, also when the store lost its crash count.

**No store connection crosses a fork.** SQLite keeps the locks of a process in its memory. A forked child that opens
the store while a copy of its parent's connection lives in it takes no real lock. Another process can then checkpoint
and delete the WAL that the child writes, and the store breaks ([How To Corrupt An SQLite Database
File](https://sqlite.org/howtocorrupt.html), section 2.6). So the hook and the worker fork through `store.fork()`. It
closes the connection of the process first, and it refuses the fork while any descriptor still holds a store file.
`db()` raises when it finds a connection of another process. The hook and the worker fork with one thread. The
listener never forks, and the thread pools of a backfill or a scan share one process, which SQLite supports.

**Time limit.** A job has 300 seconds from the file lock to mkvpropedit. mkvpropedit has no limit,
because a kill mid-write can break the header. A remux, its proof and its swap have none either, and
the checks after a remux get a new 300 seconds. The limit is a deadline, `content.DEADLINE`. Each
subprocess, HTTP call and lock wait under it ends at it, and a long read checks it between two
blocks. It raises `content.OutOfTime`, because a network handler would swallow a TimeoutError. The
metadata checks after the edit get a new 300 seconds. Every error is logged, and the import never
sees one.

**The file lock.** Every job, backfill and scan takes `STATE_DIR/lock`. An import job waits an hour at most.
Reads take it shared. An edit, a re-grab and a conversion's swap take it exclusive. Every taker
first passes `lock.gate`, so a waiting edit stops new readers and waits only for the reads in
flight. Linux flock gives a waiting exclusive lock no preference, so without the gate a scan could
keep an edit out. flock cannot upgrade a lock, so a job that takes it exclusive checks the file's
inode, size, mtime and the re-grab unit of its download again, and runs again when one changed. Hearing holds no
lock, because it may wait for the one model. A conversion in a job process builds the new file under the shared lock,
as a backfill worker does, so the other jobs keep checking and converting. The rest of that job runs under the
exclusive lock. Under the shared lock a conversion creates its temp file only when none is there. A second job of the
same file finds it, lets the lock go, and waits until the first job's swap removes it, `SWAP_POLL` (600) seconds at most.
It then runs again under the exclusive lock. A temp file that a killed job left costs that wait once, and the run under
the exclusive lock writes over it. The swap also checks that the temp file is still the one its proof read.

**One download.** A re-grab deletes the broken files of the whole download. The jobs of a download
run their checks side by side. A job is *settled* when its checks are done and no re-grab can come
from it. Before a job edits or re-grabs, it waits until every older job of the download is settled.
So a download ends the same way as with one worker. A conversion's swap does not wait. A re-grab that runs during the
app's import of the new file skips that file, and the conversion's job then checks the new file itself.

**Deep analysis.** With `SUBTITLES=deep`, an import job queues one deep analysis job for its file in the state store,
named by the path, so a newer import of the path replaces it. The job holds the inode, size and mtime of the file the
import left. The worker runs one only when no import job waits, one at a time per host, and never drops one by age. A
job drops itself with one decision line when its file is gone or has another inode, as after an upgrade, whose import
queues a job of its own. When the file is gone at its start, the job first asks the app by the file id where it is now,
see `runner.app_file()`. After a rename or a move, which queues no import, it goes on at the new path and logs a
`file_moved` line. An edit in place keeps the inode, and the job goes on. It checks this when it starts, each time
it takes the file lock, after its remux and after an error, because the app holds no lock. A change of its own never
counts. A conversion of its own before a yield gives the job the new file's inode, size and mtime. The queue drains with
no new import, because the worker runs until both queues are empty. A deep analysis has no time limit. Its whole-file
hearing hears two windows at a time, and between two of them it yields when an import's hearing waits at `lid.turn.gate` or an import
job waits in the queue. It also stops for a waiting import job after the whole-file read, before each read and each fit
of a track, after each 10 minutes of audio of the speech read, see "Incorrect subtitle identification", and before the
remux. It then goes back to its queue. Its next run finds the words heard so far in the cache, and the whole-file read
in the state store. With `HOOK_WORKERS=1` an import job so waits at most for one step: the whole-file read of one film,
the read or fit of one track, one pair of windows, 10 minutes of audio of the speech read, or one remux. With more
workers it runs in a job process of its own. A deep analysis decides with the inputs its import stored in the job: the
original language, the release name, the kids flag and the rest, so it asks no app for them. It never hears the language
again. The job names the import's job in `from`, and its decision line and syslog line carry it. It also holds the
subtitle alerts its import held for it, see "Alerts".
It keeps every flag the import set, after a remux of its own too. Only a subtitle verdict changes a flag: a track whose
words do not match the audio loses its default and forced flags.

**Crashes and stops.** A job process that dies goes back to the queue, and the third crash drops the
job. SIGTERM starts no new job, kills each child ffmpeg or mkvmerge, and puts the job back. A job
inside mkvpropedit or a re-grab's deletes blocks SIGTERM until they end. Pending Plex analyzes go into
the state store for the next worker. With `KillMode=process` on the app's unit, a stop of the
app never signals the worker. A decision log line is one `O_APPEND` write, so lines never
interleave. When the app renamed a file before its job ran, the worker asks the app for the new path
by the file's id. A 404 or another item drops the job as `file_gone`. When the file is at its path, each run of the
job asks the app by the file's id too. The run asks under the file lock before the checks, and a re-run after a re-plan
asks again. A second import of one release can delete the job's file record and write its file to the same path. A 404
or another item then drops the job as `file_gone` with no alert, and the note says the app replaced the file. The
newer import's own job checks the new file. Another API error ends the job as an error. The `ManualImport` of a
conversion gives the file a new id, and so does an undo that imports the original again. The queue keeps the new id
when the app gives one, so a re-run asks the app for it. With `HOOK_WORKERS=1` a job holds the lock exclusive and never
re-plans, so a replace during the checks still reads the copy. With `HOOK_WORKERS=1` the worker also runs a deep
analysis in its own process, so no crash count covers it. A deep analysis that kills the worker runs again at the next
start, and the subtitle alerts its import held wait. A count of starts would count each stop of the container too,
because a stop ends the worker the same way.

## State

`STATE_DIR/state.sqlite` holds the state: the job queue and the claimed jobs, the re-grab units and
counts, the records of the kept files and their folders, the pending conversions and Plex analyzes,
the `--plex-later` folders, the alert markers, the TMDB cache, the scan progress, the subtitle
hunter's tries, and the fields of each decision line that the audit reads. It is one SQLite file
in WAL mode, from Python's own `sqlite3`. A change runs in one short transaction. A transaction waits 60 seconds for another writer, or until the job's
time limit. The app waits for the hook and the listener, so each of their transactions waits 2
seconds at most, and a hook run waits 2 seconds in all. When the store does not take a job in that
time, the job goes into `STATE_DIR/queue/` as a file, and the worker moves it into the store. The
check for waiting work only reads, so it never waits. So a busy store never slows or fails an
import. A re-grab unit keys on the instance and the download id, because two instances can grab one
torrent under one download id.

The code never reads the decision log back. Each line the readers need also fills the store: an
`editing` line without its `edited` line, the time of each Plex analyze per section, and the fields
of each decision line that the audit and `--force-convert` read. The store keeps decision rows and
editing marks for 14 days, two weekly rotations of the log. `--audit --since` reads that window.
A store error loses such a fact and never stops the job.

A worker checks the store at its start with `PRAGMA quick_check`. A store that reads as no database, or fails the
check, moves aside as `state.sqlite.corrupt-<time>` with its `-wal` and `-shm` files, and a new store starts. The worker
reads the jobs of the old store from its table, without the index, and queues each one in the new store, unclaimed. A
broken store can still have taken the job of an import. The worker writes one decision line with outcome
`store_corrupt`, which syslog gets too. It says what the check found, how many jobs moved and how many rows did not
read. It posts one embed at most each day, and the file `store-alert` keeps the time of the last one. The other records
of the old store are not in the new one. A hook that finds the store broken writes its job into `queue/` and starts
the worker, so that job runs on the new store.

These stay files of their own: the decision log, `status.json` (a monitoring agent reads it),
`lid.sqlite` (the language detection reads it), the locks (`lock`, `lock.gate`, `lid.turn`,
`lid.turn.gate`, `worker.lock`, `subhunt.lock`), and the lists for a person: `convert-<app>.txt` and
the scan's `<kind>-scan-<app>.txt`, or `<kind>-scan-<app>-ids.txt` for a scan with `--ids`.

In Docker, `STATE_DIR` is `/config/state`, a named volume of its own. SQLite in WAL mode needs a local disk, and
`/config` may sit on a network share.

## Memory

The worker forks from the app, so it and its children (ffmpeg, mkvmerge, language detection) run in
the app's cgroup. Under the systemd default `OOMPolicy=stop`, an OOM kill of any child stops Sonarr
or Radarr, and `Restart=on-failure` starts it again mid-import. Set `OOMPolicy=continue` on both
units, then run `systemctl daemon-reload`. The app needs no restart, because systemd reads
`OOMPolicy` when the OOM event arrives.

```
# /etc/systemd/system/radarr.service.d/arr-media-guard.conf, the same for sonarr.service
[Service]
OOMPolicy=continue
```

## Logs

The decision log at `LOG` gets one JSON line per file a run looks at. It holds the item, the file,
every track as classified, each hearing, the plan with its rules and reason codes, and the undo
command. It also holds the audio and video verdicts, the metadata evidence, and what a re-grab, a
conversion, a header repair and Plex did. `outcome` holds the outcome code, `result` a sentence, and
`recheck` the plan right after an edit, which the audit reads. `findings` holds each finding with its
facts and its action, `alert_kinds` their kinds, and `alerts` their text, see "Alerts". A job whose file the re-grab of
its download deleted logs `deleted_with_download`, with the fault kind of that re-grab in `fault`.

The outcome, reason and Plex codes are stable, so queries and alerts can match on them. Each step
that makes a result gives its outcome code with it, and a result with no code logs `other`. The
reason codes come from the `say()` calls in
`decide.py` and from the header and restore steps. An `editing` line with the undo goes
out before each edit, so a crashed edit still has a record. A repack, a header repair and a subtitle
remux each write a line before anything else runs, with the code of the step in `outcome`. A subtitle remux
that changes no flag gives the decision line the outcome `subtitles_remuxed`, and its result names each change, as
`subtitles remuxed: s2 retimed, s1 lines moved`. When only sidecars changed, the outcome is `sidecars_changed`, as
`sidecars changed: Film.en.srt retimed`. Only a
decision line has `schema`. A missing or broken policy file never
stops the hook. It logs `no_policy` and alerts once. A backfill or an audit refuses to start. Rotate
the log weekly with compression. The code never reads the log back, see "State".

Each decision also goes to syslog as one logfmt line with the tag `NAME`. It holds no path, no track
list and no secret. The app key is `arr`, because a log store such as Loki often puts the syslog tag
in its `app` label. A deep analysis line adds `from`, the job of the import that queued it.

```
arr=radarr source=hook outcome=edited class="English original: audio switched" edits=2 reasons=kids_dub alerts="" tmdb=found label="Film A" id=65ab6056ca69
```

## Backfill

A backfill runs the hook's decision over the library. It is dry unless `--apply` is given, and it
never posts to Discord. It runs at nice 19 and idle I/O, with the hook's file lock for each file. It
runs language detection, the metadata checks and the header check like the hook, but it never
re-grabs. It reads each `.mkv` file's own tracks, because the app's stored media info goes stale. It
takes a file with two or more audio tracks, any subtitle, a tag to fix or another container inside.

A dry run reads `SCAN_WORKERS` (1) files at a time, and `--workers N` wins. The file server sets the
pace, so raise `SCAN_WORKERS` until the server is busy. An apply edits one file at a time, so `--limit` and `--canary` stay exact. Run a
long pass with `setsid nohup` and a log file. Run a large backfill as a dry run, an audit of the
plans, a canary, and then the rest.

```
arr-media-guard --backfill radarr --plan-out /root/radarr-plans.jsonl                      # dry run
arr-media-guard --audit radarr --plan-from /root/radarr-plans.jsonl --post                 # one summary of the problems
arr-media-guard --backfill radarr --apply --plan-from /root/radarr-plans.jsonl --canary 20 # a sample
arr-media-guard --audit radarr --since 2026-01-01T10:00 --post                             # the canary's edits
arr-media-guard --backfill radarr --apply --plan-from /root/radarr-plans.jsonl             # the rest
```

`--plan-from` limits an apply to the files the dry run planned a change for. Each file is planned
again before its edit, so a changed file gets the current plan. `--canary N` takes one file of each
class in turn. `--only-undecided` takes the undecided files, so hearing can decide them without a
probe of the whole library.

## Audit

`--audit` never edits. It prints one summary and, with `--post`, sends it as one embed when it found a problem. With
`--plan-from` it groups a dry run's plans by policy path and rule, counts the undecided and dropped
plans, and checks the invariants again. A plan with no undecided, dropped or skipped file and no broken invariant
posts nothing. With `--since 24h` or `--since <ISO date>` it reads the
decision log. It expects no further edit in each edit's `recheck`, and it probes an edit logged
without one.

Schedule `arr-media-guard --audit <app> --since 24h --post` each night for each app, with a systemd
timer or cron. It posts only when a file of the day has a problem: a re-check that still plans an edit,
an undecided or dropped plan, a broken invariant, a failed or skipped repair or conversion, or a probe that
failed. The post lists each file, its problems in plain words first, then the files that are OK. The name of a file
with a problem links to its page in the app, as in an alert. Its footer
names TMDB only when TMDB had trouble that day. The syslog summary line goes out every night. The audit also
removes kept originals past their age and writes the policy status, see "Status file".

`--check-audio` and `--check-video` scan every file of the app the way the worker checks an import.
A scan never deletes, edits or re-grabs. It checks `SCAN_WORKERS` files at a time, and each worker
pauses 2 seconds before a file. It keeps its place in the state store, so it resumes over days, and
`--restart` starts a new pass. Problems go to a list file, `STATE_DIR/<kind>-scan-<app>.txt`. A run that finds a problem
posts one summary embed, and a clean run posts none. On NFS, idle I/O
priority has no effect on the file server, so the worker count and the pause are the throttles.

## Reverse an edit

Each edit logs `undo`, the complete mkvpropedit command with the old values, before mkvpropedit
runs. The selectors are track UIDs, so they hold when the track order changed. monitoring.md shows how
to run the undo of a file's last edit. A later import of the same item runs the hook again. Delete the connection in the app to stop that.

## Conversion

Every video file that is not Matroska becomes `<base>.mkv`, and so does a `.mkv` file that holds
another container. `--backfill <app> --convert` converts the library. The hook converts an import
only with `CONVERT=true`, except a `.mkv` file with another container, which always converts. The
log calls a conversion a repack. A conversion keeps no original, except a forced one. It proves every
stream first, and the app must list the new file before the original goes.

**Skips.** A skip writes nothing, and the file goes on `STATE_DIR/convert-<app>.txt`.

- a hardlinked file, because the rename would split the link
- a file over `REPACK_MAX_GB` (30), a file with less than twice its size free, or a taken new name
- a file that is not the item's listed file inside the item's folder. The app moves and renames a
  file from outside the folder, and a bonus file could pass as an upgrade.
- a Sonarr name, or an extra's name, that the parse API reads as other episodes, because the rescan
  links extras by their parsed episodes
- a new name that loses a custom format that scores, a file ffprobe cannot read, or an empty
  caption track

**The remux.** A `.srt` beside the video that starts with its base name is muxed in, with its
language, forced and hearing-impaired flags from the name. Its text can overrule the name's language,
and a name with no language takes the text's. A sidecar in a legacy codepage goes in decoded right.
One that no codepage reads decodes as cp1252 and goes in as before 2.4.0, see "Subtitle text". A sidecar that ends past the video, or
has cues out of order, is timed for another cut and stays beside the file. `mkvmerge
--disable-lacing --track-order` writes the temp file into `.<NAME>-convert` beside the video. The apps' disk
scan and Plex skip a hidden folder, so they never import a partial file. A hidden file beside the
video is not enough. The app's scan takes it as an extra of the item and can rename it or send it to
the recycle bin. A swap once hid it with the extras, and the link to the new name failed. An
mkvmerge error fails the conversion. mkvmerge exits 1 on warnings alone, and then the proof decides,
because a warning can be harmless, such as zero bytes it skips at an audio end. ASF and WMV go through
`ffmpeg -c copy -copyinkf`, because mkvmerge cannot read them, and any ffmpeg message fails them.
`-copyinkf` keeps the frames before the first keyframe. A c608 caption track becomes an English
SubRip track, because mkvmerge drops it.

**The proof.** No stream is decoded. ffmpeg reads both files at once with `-c copy -copyinkf
-copyts -f framemd5` and pairs the streams of each kind. `-copyinkf` keeps the frames before the
first keyframe, which a copy drops by default. Without it, a new file that lost them would pass. A stream must keep its codec, its count of packets
with data, and a digest over every packet. Where mkvmerge changes packets on purpose, a bitstream
filter brings both sides to the same bytes (`PROOF_BSF`). Each stream must start within 0.05
seconds of where it started, and the video and audio must end within 1 second. Each packet time may
move 2 ms at most, because a stream whose times jump back can pass the digest and still play late.
Lacing is off, because ffmpeg reads the frames of a lace a few ms off. MP4 timed text becomes
SubRip, and its cues must match.

Matroska stores no decode times, so ffmpeg guesses them during its read. A frame stored far ahead of
the frames it displays after breaks the guess, and ffmpeg's muxer then moves a few packet times of
the read. So when the time check fails on the new file, ffprobe reads that stream's stored times
again. ffprobe only demuxes. The second check decides, and the proof entry names it in
`times.reread`. The second read runs only after a failed check. Stored times that moved still fail.

The proof allows a cut last frame and a cut first audio frame that mkvmerge drops, because a cut
frame cannot decode. mkvmerge may also keep only the tail of a cut first MP3 or MP2 frame. That
passes when every other packet matches and the second packet keeps its time. mkvmerge re-times the
stream from the first frame it keeps, so junk longer than one frame would make the audio play early.
The first MP3 or MP2 packet can also hold junk before a whole frame, zeros or a RIFF header of at
most 128 bytes, and the stream can end on a cut frame that mkvmerge drops. The rule runs in any
container, and the measured files are AVIs. It passes when one read of packet 0 of each file shows
the new packet 0 is the tail of the old one after the junk. The entry names the junk in
`trimmed_first.junk` and the cut frame in `dropped`. The proof also allows a last sample that an MP4 edit list hides.

A few packets more may go at the ends when every other packet matches in order. The video may lose
up to 3 packets at its end (`END_LOSS`), a loss of up to 3 frames that is accepted. The audio may lose
junk at its start, zero packets at its end, and one cut frame at each end, up to 16 packets
(`JUNK_MAX`). Junk is a packet of zero bytes, or a stray RIFF header of at most 128 bytes. A read of
the first packets shows a header, and it runs only when a lost packet before the first frame is not
zeros. The kept audio must keep its start and its times, so audio that moved still fails. The proof
entry names each lost packet in `dropped_start` or `dropped_end`, with its time, size and kind.

Three more differences pass, each with its own check:

- An HEVC codec header can hold units that mkvmerge copies into packet 0, such as an SEI. The proof
  filter takes out only the parameter sets. When only packet 0 differs, one packet of each file is
  read. It passes when the new packet 0 holds the original's and units of the header, byte for byte.
  The entry names them in `header_units`.
- An audio packet may share its time with a neighbour in the original, in any container. The
  measured case is an MP4 whose first two AAC packets carry one time, which mkvmerge spaces one
  frame apart. When the time check of an audio stream fails, each time is measured against the
  video's start. A packet that shares its time may move one frame, the median step of the stream,
  and every other packet 2 ms. Video never gets this rule. The entry names it in `times.shared`.
- An MP4 timed-text cue can start and end at one time, beside another cue at that start. mkvmerge
  gives it a length. It passes when it keeps its start and text and ends at or before the next
  cue. The entry names it in `zero_length`.

Any other lost packet fails, and the file keeps its original.

**Force a conversion.** Some refusals are safe, and only a person can tell. One example is an MP4
whose edit list hides the last frames of a still picture, which mkvmerge keeps. `--backfill <app>
--convert --apply --force-convert PATH [PATH ...]` takes only the listed files. The force is bound
to the refusal a person saw. A file converts when the proof refuses it with the same text as the
last refusal the decision log holds for its path. Another refusal, or none in the log, is not
forced, and the run reports it. Every other step runs as normal: the remux, the swap, the app's
import, the extras and the checks after them. The original is kept in `.<NAME>-originals` for `KEEP_ORIGINALS_DAYS`, as a
header repair keeps it, so one move undoes the conversion. See "Kept originals" for the hard link and its copy fallback. The option needs `KEEP_ORIGINALS_DAYS`
above 0. The decision line names the refusal in `repack.forced`, and the nightly audit says
"forced". A listed path that is not in the run's work list is reported and skipped. The hook
never forces a conversion.

A listed Sonarr file also skips `parse_refuses()` for its own new name. Scene numbering can put specials at the start of
a season, and an alias can match another show, so the parse can read a right name as other
episodes. The ManualImport names the item's own episodes by id, so the link stays right. The parse
result is fixed, so this needs no logged refusal. Every other check still runs, and the proof must
still pass unless its refusal is logged. The decision line names the parse result in
`repack.forced_name`, the original is kept as for a forced proof, and the audit says "name
forced". The extras keep the check. They go back by a rescan, which links them by their parsed
names, and a later rescan or rename could link a subtitle to the wrong episode and rename it. So an
extra whose name maps to other episodes still refuses the file, and the refusal names the extra.

**The swap.** The original must still have the inode, size and mtime it had before the remux. With
a new name, a `converting` line and a pending entry in the state store come first. The entry names the process and
its pid namespace. A later run reports an entry whose process is gone. The pid of another namespace, as of a
container beside the host, means nothing here, so such an entry counts as running for one day
(`JOB_MAX_AGE`), and as stopped after that. A restart of a container or an LXC gives a new namespace, and the
day keeps such an entry from hiding for ever. The extras move
into `.<NAME>-convert`, the new file is linked in as `<base>.mkv`, and the original moves to a held name in
`.<NAME>-convert`. The app then takes the new file. Only then are the original and the sidecars deleted. When a
step fails after that, `convert_undo()` reads the app first and never deletes a file the app lists. A
failure removes the temp file last, after the extras are back, and then the empty `.<NAME>-convert`.

**The app takes the new file.** A `ManualImport` command names the item. It carries the old record's
quality, languages, release group and indexer flags, so the app never parses the new name. The file
is in the item's folder, so the app neither moves nor renames it. The hook puts back the scene name
and Radarr's edition, because only an import from a download sets them. The apps score custom
formats on the scene name, else the download path's name, else the file name. Sonarr's API leaves out
the download path, so the hook reads it from the import history: the row with the record's file id, or
for a row an older Sonarr wrote with no file id, the row of one of its episodes within 60 seconds of
the record's date. So a new name that would lose a format scoring above 0, or gain one below 0,
refuses the conversion. On Radarr the hook
reads the record that `movieFileId` names, because `movie/<id>` shows the old record while it stays.

**The extras.** The rescan that removes the old record would send its extras to the recycle bin. So
the hook hides them during the swap. Radarr lists them in its `extrafile` API. Sonarr 4 has no API for
them, so the hook follows Sonarr's own rule. A rescan tracks each file under the series folder that is
no video, sits outside the folders a scan leaves out, and whose name parses as the episodes of one
file. So the hook walks the series folder and asks Sonarr's parse API about each such file. A file
beside the video that starts with its base name needs no parse. This covers the sidecars of any
program, such as a bonus PDF in a sibling folder. Metadata files stay in place, because the app writes
them again. On Sonarr these are the names its metadata writers claim: `-thumb.jpg`, `.xml`, `.jpg`,
`.metathumb`, and an `.nfo` with a Kodi tag.

After the import the hook rescans. The extras go back once the rescan completed and the app no longer
lists the old record. Radarr moves the extras of a dropped record to the bin in a task after the
command, so the hook also waits until Radarr's API lists no extra of the old record. Sonarr does that
inside the command, before the command completes (`ExtraFileService`, an `IHandle`). A second rescan
links the extras to the new file. Radarr's API then reads each link back. Sonarr has no API for that.

**Plex and workers.** A renamed file gets a folder scan. `--plex-later` only lists the folders, and
`--plex-flush <app>` later sends one scan per library location, which saves the idle waits of a long
run. A conversion reads the file three times and writes it once, so the file server sets the pace.
Keep `CONVERT_WORKERS` (1) low. `CONVERT_MAX_FILES` (200) stops a run after that many
conversions. A `--convert` run decides no flags, and the language backfill does that later. Convert
a library like a backfill, with a dry run, an audit and a canary first.

## Damaged source

A conversion reads the whole original, so it can show that the download is damaged. The hook then
re-grabs the import. Only these signs count:

- mkvmerge warns "This audio track contains N bytes of invalid data which were skipped" for an audio
  track, more than `SKIP_EDGE` (5) seconds from either end of the file. The proof then refuses that
  same audio stream, for its packet count or its packet data. The k-th audio track of mkvmerge is the
  k-th audio stream of ffprobe, as the proof pairs them.
- ffmpeg does not read the original cleanly in the proof, with "NAL unit size", "Invalid data found" or
  "partial file". A failed read of the temp file says nothing of the original.
- ffprobe reads no stream in the original and names a data error, "Invalid data found", "moov atom not
  found" or "partial file". An empty message or a read error of the file system is no damage.

A proof refusal alone, over an edit list, the times, the cues or a packet count, is no damage. So is a
warning alone, or a warning with a refusal of another stream. A skip near an end is often junk, such as
zero bytes after the last audio frame, and a clean proof converts that file. Each of these stays a
refusal with its "Conversion to MKV failed" alert, and the original stays.

The re-grab uses the steps and safeguards of "Broken audio". They are the grab record, `REGRAB_CAP`,
the download as one unit, the second check and "Restore after a bad upgrade". The second check runs
the read that showed the damage again, from scratch. mkvmerge remuxes into `/dev/null` and must skip
invalid data in the same audio stream, away from the ends, again. ffmpeg reads the original through the
proof's filters, and ffprobe probes it. The proof does not run again, and its refusal stands. The
re-grab judges only the job's own file. Another file of the download shows its damage in its own
conversion, and joins the unit then. A re-grab that deletes the file ends the job. Otherwise the
original stays and gets the import's audio and video checks. The decision line has the outcome
`damaged_source`, the `regrab` code and `repack.damage`. One red "Damaged file" embed says what the
hook did.

Only a hook job re-grabs. A library backfill with `--convert` lists a damaged file in
`convert-<app>.txt`, and it never re-grabs. The re-grab needs `damage` in `REGRAB`, which is off by
default. Without it the hook checks the original again and posts "Damaged file, re-grab is off". A
file ffprobe cannot read is skipped and listed, with no "Conversion to MKV failed" alert.

## Header repair

A Matroska header can be wrong while every frame is whole. A lossless mkvmerge remux writes the
Segment duration, the Cues and the Segment size again from the streams. Real damage is never
repaired and keeps its re-grab. `HEADER_REPAIR=false` turns the repair off, and the check still
reports.

`header_probe()` reads the file start and the Cues. It reads the last Clusters too when the Cues are
missing or the Segment size runs past the file end. It also reads them when the last cue is far off
the header duration, or when over 1 MiB (`TAIL_MIN`) follows the Segment end. When a subtitle may
run past the end, ffprobe reads every subtitle packet. A remux fixes missing Cues, a wrong duration,
a Segment size left by the hook's failed edit, and a tail. A SubRip line past the end gets the trim
or the removal. Another subtitle codec past the end has no fix and posts one amber embed.

The remux runs only when the video check ran all its stages and found no certain fault and no doubt.
The video and the audio must also end at most 60 seconds apart. It runs like a conversion, but it
keeps the name and the original, and it keeps the track order and every UID. The new file must pass
these checks:

- mkvmerge exits 0 with no warning.
- The same tracks, in the same order, with the same properties and UIDs.
- The duration within 1 second of the last block, usable Cues, and no issue left.
- A video frame count of at least the end over the frame duration, less 3 frames. mkvmerge can drop
  a zero-filled Cluster with no warning, and this catches it.
- A size within 3 percent of the original Segment, and 3 clean decoded windows.

**The trim and the removal.** The trim runs when some track ends over 60 seconds or 2 percent past
the video and the audio. It cuts the late SubRip lines at the end. A track with at least
`REMOVE_MIN` (2) lines, and `REMOVE_SHARE` (10 percent) of its lines, after the real end is timed
for another cut. The hook removes that track when it also ends past `REMOVE_END` (1.2) of the listed
runtime, because a wrong track is worse than none. It trims the other late tracks. A PGS or VobSub track would need a new encode, so it
stays.

**A cut file keeps its subtitles.** A cut file's subtitles run to the full length and would meet the
removal rule. So nothing changes when the video and the audio end under `CUT_END` (0.95) of the
listed runtime, or when no runtime is listed. One amber "File may be cut short" embed goes out instead. It
also covers a track from another episode, so check the last frames by hand.

**The tail.** A download can write a shorter copy over an old file and leave the old bytes past the
Segment end, and some players decode them. The remux cuts the tail when the video and the audio
reach the header duration. A Segment that is itself cut stays, with a video doubt. Bytes that start
with the EBML id `1A45DFA3` are a second file joined with `cat`, and they stay.

A failure keeps the original and posts one amber embed, and the flag edit still runs. On success the
decision runs on the new file, and the app rescans the item. A backfill dry run reports
`would_repair_header`, and `--apply` repairs.

## Kept originals

A header repair, a tail cut, a trim and a subtitle removal keep the file they replaced for
`KEEP_ORIGINALS_DAYS` (7). 0 drops it at once. A conversion keeps nothing, except a forced one and one that leaves out
a subtitle track that does not match, see "Subtitle match". The original is
hard-linked to `<folder>/.<NAME>-originals/<UTC time>/<path from the folder>`. A hard link needs no space and never
leaves the path missing. `keep_root()` picks the folder, the same way for `.<NAME>-recycle`:

1. The top of the mount that holds the file, when `.<NAME>-originals` exists there and is writable.
2. The mount top, when it is writable. With the app root folders and the Plex sections below the mount, nothing
   scans the folder.
3. A writable `.<NAME>-originals` that exists on the path from the mount top down to the file's folder, the highest
   first. So the place stays the same from run to run.
4. The highest writable folder on that path. A Docker volume whose root only root may write needs this.
5. The mount top. No folder on the path is writable, so the fix is skipped, and the reason names the mount top, the
   uid and the gid.

Each folder sits on the file's mount, so the hard link works. A folder whose real path is on another mount, as one
above a symlink to a share, never comes in. The dot hides it from the apps' disk scans and Plex. When the app renames
an item folder that holds such a folder, the folder moves with it, and the prune and the restore no longer find it.
Sonarr's Library Import lists a hidden folder at a root folder's top level as unmapped. The state store
records each folder the hook keeps a file in. The nightly audit and the worker's daily prune clear those folders too,
and a folder of another name never.

Some file systems refuse a hard link. A keep folder on another file system does, and so do a file with too many links
and some container or Windows mounts. The original is then copied, with its mode and times. The copy needs the file's size
plus 1 GB free, else the fix is skipped and says why. It is written under a hidden name, compared with the original
by size and hash, and only then renamed to the kept name. So the swap waits for a whole copy, and a crash leaves no
file that looks kept. The log and the report of `--sub-time` say "copied" instead of "hard-linked". Any other link
error skips the fix. To undo a repair, move the kept file back and rescan the item.
`NAME` names the hidden folders, `.<NAME>-originals`, `.<NAME>-recycle` and `.<NAME>-convert`. A `NAME` with other
characters than letters, digits, `.`, `_` and `-` fails `--selftest`, and the script uses `arr-media-guard`.
Each keep and the nightly audit remove the time folders older than the setting.

## Subtitle hunter

Some foreign films have no English subtitle track. The hunter replaces such a file with a release
that has a full English subtitle. It handles Radarr only, because an episode needs its own search. A
release that fails must cost a download, never the current file. So the hunter downloads outside
Radarr, checks the file, and keeps a hard link of the old file until the new one is in place.

`arr-media-guard-subhunt radarr --ids 101` lists the ranked candidates, and `--apply` downloads, checks and
imports. The hunter needs a Newznab indexer, such as NZBHydra2, and a SABnzbd download client in
Radarr. It reads their URLs from Radarr's API. The API masks their keys, so the hunter reads them from
`SABNZBD_API_KEY` and `NEWZNAB_API_KEY`. SABnzbd's download path goes through Radarr's path map, so
SABnzbd must see the downloads at the path Radarr sees. Radarr gets the path back through the same map.

**Search and rank.** Each movie gets one indexer query by IMDb id. A result must carry this movie's
IMDb tag, else Radarr must map its name to this movie. The tag comes first, because Radarr can map a
name to another film of the same title. The profile must allow the quality at no less than the
current resolution. The size must fit the runtime. An English subtitle tag scores 4, a streaming
source 3, Criterion 2, and multi or dual audio 1. A local scene tag scores -2, because those
releases carry local subtitles. The patterns are `SIGNALS` in the code.

**Download and check.** At most 3 candidates per movie go to SABnzbd, with no category, so Radarr
never sees them. SABnzbd fetches every NZB at once, because an indexer link can expire. A download
passes when it has an English full or SDH subtitle and runs within 10 percent of the listed runtime.
Its main audio must be in the original language and decode in three samples.

**Import.** The hunter stops when Radarr changed the movie's file meanwhile. It hard-links the
current file to a hidden keep name, and the state store records the link first. The import is a
ManualImport in copy mode, because a move can fail on a network share after Radarr deleted the old
file. When the new file landed, the hunter removes the link and the download, and logs the import with no post. A
keep link it cannot remove then posts one amber embed. Else the link goes
back, the movie is marked `import_failed`, and a red embed asks for a rescan. A keep link that still
exists stops every later run for that movie.

**Give up.** A failed candidate goes into the hunter's state in the state store, and Radarr's blocklist
stays as it is. When every candidate fails, the movie is marked `no_subbed_release` and keeps its
file. One amber embed names the other option, an external `.srt` from Bazarr or OpenSubtitles.
`--force` hunts again. Before the first apply, check that a hard link works on the media share
(`ln`, then `stat -c %h` prints 2). Run an apply with `setsid nohup`, because a download can take
long. A later Radarr upgrade can replace the hunted file, so run the hunter again then.

## Metadata checks

`content.py` checks the app's metadata before a language or runtime alert trusts it. It also adds
up the evidence for a wrong-content re-grab. A correct file must never be deleted. The hook runs the
checks after the flag edit.

- **Languages.** English, the item's original language as the app lists it, and TMDB's original are always right. TMDB's spoken
  languages are right when TMDB's original is not English. For an English original, audio in a
  spoken language counts as unknown.
- **TMDB.** Each answer is cached for 30 days. A failed call pauses TMDB for 10 minutes, so an
  outage costs one timeout per run, and a failure reads as unknown. By default the module reads
  Radarr's bundled TMDB key from `/opt/Radarr/Radarr.Common.dll`, so a host follows a Radarr update.
  Without that file, as in Docker, it takes a copy of the key from Radarr's source. `TMDB_TOKEN`
  overrides both. The `tmdb` code of a record is `found` (TMDB returned the item's record),
  `no_record`, `tmdb_unavailable` or `tmdb_token_rejected`. A record of an older release can hold
  `tmdb_token_missing`. The syslog line says `not_asked` when the run asked
  TMDB nothing, as in a conversion backfill. `status.json` and the nightly audit keep `ok` for a day
  or a check where TMDB works. A cached answer never counts as live, so it never hides a dead key.
- **Duration.** A duration is trusted when two sources agree within 30 seconds or 2 percent. The
  sources are the header, the stream durations, the size over the bitrate, and the last video
  packet. A header that no other source confirms gets a duration alert only.
- **Runtime.** A movie is short under 0.6 and long over 1.4 of its listing. An edition word such as
  Extended or Director's Cut widens that to 0.5 and 2.0. A TV movie or a stand-up special gets 0.5
  and 1.8. A multi-cut file is never long. An episode is judged on the short side only, and only
  with a listing of 20 minutes or more.
- **Year.** The check reads the years before SxxEyy or the first quality word. It skips a year that
  belongs to a title and a daily show's air date. A year within one of an item year is ok.
- **Episode title.** A release can follow another episode order than Sonarr, so a file can hold
  another episode with the right language and runtime. The title comes from the scene NFO beside
  the file Sonarr imported from, else from the release name between the episode tag and the first
  quality word or release flag, such as REPACK or a language in capitals. A flag in title case is a
  title word, as in "Internal Affairs". In a name all in capitals, only the flags right before the
  quality word are cut, so "FRENCH WEEK" stays. A tag may name one segment
  of a DVD order, as S04E15a. In a name with no quality word, a last "-GROUP" is the group. The hook reads the NFO at the event and keeps the title in the job, because a usenet
  download folder can be gone when the worker runs. The Webhook gives the folder as
  `episodeFile.sourcePath`, and Sonarr's path map maps it. With a map set, the listener reads only
  under the map's local folders. An NFO counts only when it is a plain file whose name is the
  video's, or the one NFO in a folder named like the video or the release. When the job holds no NFO
  title, the worker reads the release NFO that Sonarr copied beside the video. Sonarr copies it when
  its Import Extra Files setting lists `nfo`. It names it `<name>.nfo`, or `<name>.nfo-orig` when a
  metadata writer such as Kodi writes its own `<name>.nfo`. A Kodi `.nfo` is skipped. A backfill and
  `--sub-time` read it the same way. A file with no scene name
  gives the title of Sonarr's own file name, and the alert says "the file name", because
  Sonarr wrote that name in the order it had at the import. The check then reads the series'
  episodes, one call per file, and one per series in a backfill. A file that no episode points to
  gets no verdict.

  A title matches an episode when their keys are the same: lower case, no accents, no apostrophes,
  `&` as "and". Each segment that `/` or `+` joins matches on its own. A release name joins two titles
  with no mark, so the titles that cover it from end to end, with only "and" between them, match
  too. The verdict is "other" only
  when the title matches another episode and none of the file's own. A key that two episodes share
  names neither. A special counts only for a special, because a special often repeats a regular
  title. A title within 0.8 of the file's own, by difflib's ratio, names the file's own, such as a
  title that leaves out "Part Two". So does a title that holds the words of the file's own in a row,
  or whose words the file's own holds. Neither rule joins two parts of one story, such as "Versus the
  Ring" and "Versus the Ring Part 2", so a swap of two parts shows. A title that drops its part
  number then alerts. When Sonarr imported the release to other numbers than its tag,
  its scene numbering already maps the release's order, and the title says nothing. The alert names
  the imported episode with Sonarr's title, then the episode the release's title belongs to, in
  Sonarr's order, with its absolute number for anime.

**The re-grab rule.** A re-grab needs two points. The wrong language scores one. So does a release
name that names that language, for movies only. A short or long runtime scores one, and so does a
year mismatch. Another TMDB film that the release's title and year find, whose runtime matches the
file, scores one. A release with an edition word never searches, because TMDB lists some cuts as
films of their own. A movie under half its listing scores two on runtime, when a second source and
TMDB agree. A special never does, because its listing may count the broadcast slot. A series never
gets the release-language point, because a docuseries changes language per episode. The episode
title scores `EPISODE_TITLE_POINTS`, 0, so it alerts "Maybe the wrong episode" and never re-grabs. A re-grab on
it would raise that constant.

**The switch.** `REGRAB` leaves out `content` by default. Then the hook still checks the grab
record, the cap and a second check. It then logs `would_regrab`, posts "Wrong content, re-grab
is off" and deletes nothing. Read those posts for a while before you turn it on. A verdict judges
only files of the job's own item. The second check probes the file again, hears the audio past the
cache, and asks TMDB through an empty cache.

## Audio language detection

`lid.py` hears the spoken language of one audio track. The hook calls it for a main audio track
whose language is in doubt, see "What it changes" and "Language tags".

**How it hears.** ffmpeg cuts 30-second samples, mono at 16 kHz, at 25, 50 and 75 percent, which
skips intros and credits. Silero VAD (voice activity detection) keeps the speech. A sample with
under 8 seconds of speech is dropped, and the next cut comes from another position. Sampling stops
once three samples hold speech. faster-whisper names the language of each one. It uses the `small`
model, int8 on CPU, because `small` names a wrong language less often than `tiny` and `base`. A
sample under 0.6 names no language. The module answers only with two or more votes, all for one
language, averaging 0.8 or more. Otherwise `lang` is null, and `why` names the rule that failed. The onnxruntime of the
Whisper venv writes an empty `/tmp/mat-debug-<pid>.log` on each start. The hook removes the one its own hearing left.

**What it cannot hear.** Whisper does not know Irish, Scottish Gaelic, Mixtec, Zulu, Xhosa, Quechua
or Kurdish, and it names a neighbour for them. It knows Belarusian but gets it wrong (`WEAK`). When
the tag or the original language is one of these, the module answers null and runs no model. Whisper
also confuses close relatives, such as Galician and Spanish, or Hindi and Urdu. So an answer that is
a relative of the tag or the original language (`KIN`) is withheld.

**Limits.** Hearing a file takes tens of CPU seconds and several hundred MB of memory. The CLI sets
`oom_score_adj` to 1000, so the kernel kills it first under memory pressure. Set
`OOMPolicy=continue`, see "Memory". One model runs per machine at a time. The workers of a backfill
ask for it one at a time, so a hook job waits for one hearing at most. In a hook job all tracks of a
file share 120 seconds, cut to the time left less `LID_RESERVE` for the audio samples. With too
little time left, or a file under 60 seconds, the hook skips detection. No answer leaves the
decision as it was.

**Cache.** `STATE_DIR/lid.sqlite` keys each answer by path, size, mtime, stream, model and duration.
The model name carries its revision, so a model bump misses the old rows. A hit applies the current
thresholds, so a threshold change needs no new hearing. An edit changes the mtime and never the
audio, so the hook carries the rows over after each mkvpropedit.

**Install.** `arr_lid.requirements.txt` pins faster-whisper and its dependencies by hash. The model
is `Systran/faster-whisper-small` at revision `536b0662742c02347bc0e980a01041f333bce120`, in
`LID_DIR/models/small`. `--fetch` downloads it once and checks its sha256. The model loads with
`local_files_only`, so the module never reaches the network. Create `LID_DIR/ready` last. The hook
uses detection only when that file exists, so it never uses a half-built install.

```
python3 -m venv /opt/arr-media-guard-lid/venv
/opt/arr-media-guard-lid/venv/bin/pip install --require-hashes --only-binary=:all: -r arr_lid.requirements.txt
/opt/arr-media-guard-lid/venv/bin/python arr_media_guard/lid.py --fetch --model-dir /opt/arr-media-guard-lid/models
touch /opt/arr-media-guard-lid/ready        # last
```

## Subtitle text

`text_language()` in `decide.py` names the language of a subtitle text. It needs no model and no
dependency. Each of 18 languages has a list of common dialogue words: English, Spanish, Portuguese,
French, German, Italian, Romanian, Dutch, Afrikaans, Swedish, Danish, Norwegian, Polish, Czech,
Slovak, Turkish, Russian and Ukrainian. A word in one list only is a *telling* word. Its answer is the *read* language.

- The letters give the script first. Greek, Hebrew, Arabic, Persian, Thai, Korean, Japanese, Chinese,
  Hindi, Tamil, Telugu, Georgian and Armenian text gets the language of its script. Latin and Cyrillic
  text goes to the lists. Serbian and Macedonian share most Russian stopwords, so one of their own
  letters (ј, љ, њ, ћ, ђ, џ, ѓ, ќ, ѕ) rules out Russian and Ukrainian.
- Chinese text must also hold common Chinese characters (`HAN_COMMON`), 20 percent of its Han
  characters at least. Dialogue holds far more. The bytes of another codepage decoded as Chinese give
  rare characters.
- Text whose top script holds under 75 percent of the letters is mixed at once. Between 75 and 90
  percent the count reads on, as it does for the lists. Greek dialogue can hold a few names in Latin
  letters early.
- The top language needs 90 percent of the telling words and 25 telling words at least. Its whole list
  must also hold 20 percent of all words. A language with no list stays under that share.
- The count runs every 300 letters. It stops at the first verdict, or at 3,000 letters, about 600
  words, so clear text stops early. Text under 300 letters is short. Short text, mixed text and a
  language with no list get no answer. Text whose top language holds under 75 percent is mixed at
  once. Between 75 and 90 percent the count reads on, because a few early words weigh most.

The read language is a signal, never an authority.

- **Sidecars.** A conversion already reads each sidecar. A name with no language takes the language
  its text reads as, and `und` when it reads as none. Examples are `Movie.srt`, and the `Movie.1.srt`
  that Radarr writes when it cannot parse a sidecar's language. Such a sidecar then joins the subtitle
  match as a named one does. When the text reads as a language other than
  the one its name gives, the sidecar is muxed with the text's language. The forced flag of the name
  then stays only when the text's language is a main audio language, because a player shows a forced
  track in the audio's language by itself. The name wins when the lists cannot name its language.
  `repack.sidecars` logs `read` and the `mismatch`.
- **Subtitle character set detection.** It picks the character set of a sidecar's bytes. A sidecar is UTF-16 by its BOM,
  and UTF-8 when its bytes decode as UTF-8. Else it is in a legacy *codepage*, a character table from before Unicode,
  with one or two bytes for each character. The hook decodes it with each codepage of `CODEPAGES` in turn: Big5 (cp950),
  GBK, Shift JIS (cp932), Korean (cp949), and the Windows codepages, cp1252 first, then cp1250, cp1251 and cp1253 to
  cp1257. The first decode whose text reads as a language that codepage writes wins, and the codepages of the name's
  language go first. Hebrew bytes decoded as cp1253 give Greek letters, but with ς inside words, which Greek writes only
  at a word's end, so that decode reads as no Greek. When no decode reads as a language, the first codepage of the
  name's language whose decode gives the letters of one script wins, in Latin letters mostly ASCII ones. So a Serbian
  name takes cp1250 for Latin letters and cp1251 for Cyrillic. Else the bytes decode as cp1252, as they did before
  2.4.0, and the sidecar keeps its name's language or `und`. The same decode reads a sidecar beside a Matroska file for
  the subtitle check. mkvmerge gets the codepage with `--sub-charset`.
- **Subs folders.** The hook reads the sidecars beside the video only. The apps import the sidecars of
  a download's Subs folder beside the video, under the video's name, so no Subs folder reaches the hook.
- **Tracks in a Matroska file.** The read language is one signal of the rule in "Language tags". Only
  the text tracks a decision depends on are read. These are a track that is default or forced after
  the plan, and a track whose tag the text can change: every `und` track, and a track whose two
  signals disagree. The text alone never changes a tag, except on an `und` track, see "Language
  tags". The flag rules then use the new tag.
- **A wrong tag with nothing to back a new one.** The track keeps its tag and gets a "Subtitle
  language" alert and the reason `subtitle_text_mismatch`. When no main audio track speaks the read
  language, the track also loses its default and forced flags (`subtitle_text_muted`), because a
  wrong subtitle is worse than none. The decision then counts the track as its read language, so the
  normal rules can give the default to the real English track instead. Text in a main audio language
  keeps its flags, such as forced English text tagged French under English audio.
- PGS and VobSub tracks are pictures, so they have no read language.

**The read.** mkvmerge and ffmpeg write a cue entry for every subtitle block. `subtitle_read()` reads
the Cues, which the header check has read just before. It finds the entries of the wanted tracks by
their bytes, so it never walks the other cue points. It then reads each block by its entry, with one
small unbuffered read, and stops at the verdict or after 200 blocks. It never reads a Cluster in full,
so the cost does not grow with the file size. A track with no cue entries, or with a content encoding
other than zlib, gets no answer. So does a file from a muxer that writes its cue entries in another
order or size than mkvmerge and ffmpeg. The decision log holds each answer in `read`.

## Garbled subtitle repair

An old muxer could take a sidecar in a legacy codepage for cp1252. It wrote each byte as the character
cp1252 gives it, so Greek "Καλημέρα, φίλε μου" became "ÊáëçìÝñá, ößëå ìïõ". Such text is *garbled*. The muxer also
cut a cue at a byte cp1252 leaves undefined: 0x81, 0x8D, 0x8F, 0x90 and 0x9D. mkvmerge still does. Garbled subtitle
repair gives such a track its text back, or takes it out of the file when it cannot.

**Where it runs.** `--sub-check`, `--sub-time` and the deep analysis check every SubRip track of a
Matroska file. An import does not. The muxes that garbled text are old, and old mkvmerge versions
wrote no Cues entries for subtitle blocks. So only a read of the whole file reaches their text. These
runs make that read, and an import never does.

**The check.** The cp1252 bytes of garbled text are the sidecar's bytes. `decide.recode()` *reads them back*: it decodes
them as UTF-8 and as each other codepage of `CODEPAGES`, see "Subtitle text". A character that cp1252 does not write,
such as a "♪" a later tool added, stays as it is, and the text around it reads back. A reading counts when every cue
decodes, it changes 1 percent of the letters at least (`GARBLE_SHARE`), and its text reads as a language the codepage
writes. Text in its right encoding changes only in a few borrowed words. A UTF-8 reading counts with no language too,
because text in another codepage holds no pairs such as "Ã©" that decode as UTF-8. A track of 300 letters or more is
garbled when a reading counts. A UTF-8 reading wins over every other. Else the reading in the language of the track's
tag wins.

**The repair rule.** A garbled track is repaired when its reading reads as the language of its tag.
Else it leaves the file, see "A track that cannot be repaired". The rule holds for Latin text such as
French, whose garbled words still read as French, because most of them are plain ASCII.

**The word check.** The word check of "Subtitle match" compares the heard words with the cue text, so it cannot judge
garbled text. A garbled track whose words do not match keeps its place: its verdict becomes `unknown` with `held`, and
the repair runs. The track's new times and flash ends wait too. The run then stays pending in the subtitle cache, so the
next run judges the readable text. A track whose read was cut short waits too, but it is never repaired, so it only
alerts and nothing is pending, see "Long subtitle track handling".

**The repair.** One remux of `resub()` writes the new text, and the packet proof checks it. The proof
rebuilds the text from the same inputs, so it shows that the remux wrote the plan, never that the plan
is right. These guards carry that:

- The track's own text is read back. In a one-byte codepage a character keeps its garbled form when its read-back would
  be neither a letter the tag's language writes (`ALPHABETS`) nor a symbol such as "№" or "€" (`SYMBOLS`). A right
  "Señor" in Romanian text then stays, where cp1250 would give "Seńor". Marks such as "ˇ" and "˛" are no symbols there,
  so a right "¡" or "²" stays too. A Latin letter right after an ASCII digit stays, as the "º" of "40.5ºC", unless a
  lowercase letter follows it. Then it starts a word joined to a number, as "9876şase". In Cyrillic, Greek, Hebrew and
  Arabic text a word that holds ASCII letters keeps its letters, as a right "Café". A garbled word of those scripts
  holds none, because their codepages write every letter above 0x7F. `decide.kept()` holds these rules.
- A cue cut inside a character gets U+FFFD there and counts as cut. A cue cut between two characters
  only ends early, and nothing marks it. In a one-byte codepage every cut is of that kind, such as at
  Ť and ť of cp1250, so `REPAIR_CUT` cannot count them. The repair loses nothing the garbled track
  still held. When more than 2 percent of the cues are cut (`REPAIR_CUT`), the track is not repaired. The
  remux counts again over every cue it writes, and a cue that does not read back stops it.
- A track whose read was cut short is never repaired or taken out, because its later cues were never
  judged. Its verdict holds `capped`, and it only alerts.
- A sidecar beside the file fills in the cues the muxer cut short when it is the track's source. Its name and its text
  give the track's language, and it holds the track's cues at their times. Every other cue equals the read-back. At a
  cut cue the read-back is the start of its text, and its next character holds a byte cp1252 leaves undefined. So a
  garbled sidecar, the track extracted again or an SDH version never fills a cue. A sidecar that changed between the
  check and the remux stops the remux.
- The repaired text must read as the tag's language, and the check must find no reading in it.
  Text garbled twice reads back once into text that is still garbled, so it is not repaired. It leaves
  the file as a track that cannot be repaired, see below.
- The proof reads the text of a repaired track. Each cue must be the repaired cue, at the original
  cue's start and end. Every other stream keeps every packet, as in any remux.
- The track's default flag goes off in the same remux when the flag decision turns it off, in a run
  whose flag edit acts on the decision. The deep analysis and `--sub-time` on a file no app lists keep
  every flag, so they keep it too.
- ffmpeg writes the remux, with a Cues entry for each subtitle block. An import can read the tracks
  after it. ffmpeg also writes back the bytes an old mux cut from the start of each frame and kept once
  in the track header (*header removal compression*). The proof reads the packets whole on both sides,
  so it still shows every packet the same.
- A repaired track gets no other change in the run, such as new times. A later run judges its times.

**A track that cannot be repaired.** A track the check finds garbled but cannot repair leaves the file
in the same proven remux, as a removal does (`subtitle_garbled_removed`). The original is kept for
`KEEP_ORIGINALS_DAYS`, so with `0` the track stays and alerts. Its bytes go beside the video as
`<name>.<language>.garbled.txt`, which players skip. A taken name gets a number, as `.garbled.1.txt`, and
no file is ever written over. The remux's ffmpeg read writes the track's packets as they are into a
SubRip file, and `proof.packet_text()` shows each cue's bytes equal to its packet's md5. The copy beside
the video is read back and must hold the same bytes. A dry run, `SUBTITLES=check` and a track with a cut
read only alert. A track the check misses is never touched.

The decision log holds each verdict under `garbled`, and `later` on a repaired track whose times or
words wait for the next run. The remux names the repaired tracks in `subremux.recoded`, with the reason
`subtitle_repaired`, and the tracks taken out with their file names in `subremux.stripped`.

## Subtitle match

A text subtitle can hold the lines of another episode or another cut, and so can a sidecar. `subsync.py` checks a
text subtitle against the audio. The owner's rule is "rather no subtitle than a wrong one".

**What it checks.** A SubRip, ASS, SSA, WebVTT or MP4 timed-text track in a full, SDH or dub role, a `.srt` sidecar in a
conversion, and a `.srt` sidecar beside a Matroska file, where a program such as Bazarr writes its downloads. Its language must be the
language of a main audio track. The check hears the track that plays when it speaks that language, else the first main
track that does. A forced, commentary or picture track, a forced sidecar, a track in another language and a file under
5 minutes are never checked. Nor is Japanese, Chinese or Thai text, which has no spaces between its words, so the
check could never give a verdict. The check of a conversion takes the duration ffprobe reads, because mkvmerge gives an
MP4 none. So a file with no such track or sidecar costs nothing. The check runs on imports, and a
backfill runs it only with `--sub-check`. `SUBTITLES` sets what an import does with it: `off` runs no check, `check`
reads, reports and alerts and changes nothing, `fix` acts, and `deep` (the default) adds the deep analysis. An unknown
level acts as `check`. `--sub-check` and `--sub-time` ignore `SUBTITLES`, because a person asked for them. Without
`--apply` they report, and with it they fix. It needs language detection, see "Audio language detection".

**The read.** `subtitle_cues()` reads every cue with its start and end, by the Cues, as `subtitle_read()` does. A cue
ends at its BlockDuration. A sidecar and an MP4 track are read as SubRip text.

**Long subtitle track handling.** It bounds the read of a long track. The read stops at 100,000 blocks a track
(`CUE_MAX`, and `PICTURE_MAX` for PGS and VobSub), or after 2 minutes (`READ_WALL`) on a slow share. A typeset fansub
track, or a PGS track redrawn every frame, can hold tens of thousands of blocks, so a lower cap cut real tracks short.
The whole-file read of a track the Cues do not index keeps the same cap, and stops after 30 minutes (`FULL_WALL`). The
read marks a track it cut short (`subtitles.Cut`). A block that gives no cue still counts toward the cap. The word
check and incorrect subtitle identification then judge no window and no speech past its last cue read. Such a window
holds none of its cues, and its words paired by chance with cues far away, at offsets near -2,000 seconds. Incorrect
subtitle identification judges such a track as a file that ends at its last cue read. A remux rewrites every cue of a
track, and a plan of new times or text would hold only the cues read. So such a track gets no fix of its own cues: no
whole-file timing, flash or garbled fix. A whole-track fix of the word check or a reference still moves
every cue. Foreign subtitle timing gives such a track no fix: on a review's plants the lines past the read were in time
again, and a whole-track fix moved them off. Nor does it time another
track, because that fit would rest on its first part alone.

**The hearing.** The check picks two windows of 10 seconds, one between 5 and 25 percent of the file and one between
75 and 95 percent. So the line through them covers most of the file, and a drift shows. Each window is the place with
the most cue words, because dense cues mean speech. Song lyrics (♪) do not count, and the test for them runs after the
tags are gone, because a font colour holds a #. `lid.listen()` hears both windows as one clip, with the pinned
model, greedy decoding, word times and one thread. One clip runs the encoder once, and its chunk is as long as the
clip, because Whisper's padding to 30 seconds cost decode time. Two windows of 12 seconds cost more to decode on dense
dialogue, and two of 15 made the encoder run twice. The words are cached by path, size, mtime, stream, model, language and
windows, and an edit or a proven remux carries them to the new file.

When one window hears under 8 content words and the other does not, `listen()` hears a window of 24 seconds in the same
part of the file, where it does not overlap the first, in the same process. The two windows with enough words then
decide. A file whose windows both hear enough pays nothing for this. Two windows that both hear too little get no
third, because one more window cannot give two good ones.

A subtitle timed for another frame rate drifts. At 25/23.976 the speech of a cue at 20 minutes is 50 seconds earlier
in the audio, so a window at the time of dense cues can hear silence. When fewer than two windows hear 8 words, a drift
hearing follows. For each part with no window that heard enough, it hears one window of 10 seconds where the faster
ratios, 25/23.976 and 25/24, put the speech of that part's densest cues, and one where the slower ratios put it. A
window whose words matched says where the cues sit, and the drift windows go through that point. Else the cues start
with the audio. A drift window closer than 5 seconds to a window heard already is not heard.

A release can mux the subtitle of another cut. Its cues then sit minutes late at a ratio of 1, where no far ratio
puts a window, and the track runs past the end of the audio and the video. After the drift hearing, a part can still
hold no window that heard enough. A window that heard 8 or more words, with at least 50 percent of them matched, counts
toward a match. When such a window puts the cues late and the track runs past the end, its offset puts the speech of
that part's densest cues at their start less the offset. One more hearing then takes a window there, and one where
each far ratio through that window puts the speech. A window that ends after the audio and the video end is not heard.
The header probe reads those ends from the last clusters. A file that ffmpeg wrote has no DURATION tag, and the late
track sets its header duration.
A window closer than 5 seconds to a window heard already is not heard either. A late track whose tail is in time ends
inside the video, and a fix through one window would move that tail early. So that track gets no such hearing. A
chance match of 8 or more words at a wrong offset still costs this hearing. It runs once at most.

Only the first hearing names a mismatch. Its windows sit where the cues are dense, so a wrong track shows there. A
later window can sit where the track holds no cue, as in a song or a scene the subtitle leaves out. A mismatch that
only later windows show is unknown, and the track stays.

The language check and the subtitle check share their work. The language check keeps its samples for an hour when the
file has a text subtitle, and a window inside a kept sample is cut from it, so no audio is decoded twice. When the
language check runs on a file, it runs the subtitle check's hearings after its own, in the same process, with the model
it loaded. The hook's own check then finds those words in the cache.

Whisper can loop and write one word or phrase again and again. Each segment decodes on its own, not on the text before
it. A segment whose text compresses more than 2.4, faster-whisper's own threshold, is a loop and drops. A phrase of up
to 4 content words said again right after itself counts once. "I'm sorry." said four times compresses too little to
drop, and it counts as two words. So a loop never reads as a mismatch. A real chant counts once too. That only makes
the heard side shorter, and each word of it still matches the cues.

**The verdict.** A window's overlap is the share of its heard content words that match the cue words in order at one
offset. Stopwords, one-letter words, tags and sounds in brackets drop out. The search tries the offsets that the shared
words point to over the whole cue list, so a shifted or drifting track still matches. A window with under 8 heard
content words names nothing. Two windows at 50 percent or more is a match, and two at 30 percent or less is a
mismatch. Anything else is unknown. So is a track under 20 cues, text with no spaces, a failed hearing and a timeout.
The compare holds to one time window. Over a whole file, common words and names give a wrong track too many matches.

**A translation is not a mismatch.** A right subtitle can translate other speech than the audio it is compared with.
In a series with Japanese audio and an English dub, the full English subtitle translates the Japanese, and the dub
script uses other words. Such a track can read as a mismatch, and on real files some did. So a mismatch can count as
unknown:

- When the item's original language is known, from the app or TMDB, a mismatch counts unless the original is another
  language than the subtitle. So an English original with a Spanish dub keeps the check, and a Japanese series or a
  dubbed film whose original the file does not carry gets unknown.
- When no original language is known, another main audio language in the file stands in for it. A mismatch then
  counts as unknown when the file carries one.

A mismatch held this way stays in the decision record as unknown, with the reason. It gets no alert, no flag change
and no removal, because no one can act on it. A match and a timing fix still count, and the dub's own transcript, the
"Dubtitle" track, still matches. The same rule holds for sidecars and in a conversion.

**Timing.** For a match, the check compares each cue start with the first heard word of the cue. Each window's median
gives a point, and the line through the first and the last point says where the cues sit at the file's start and end.
Only a cue whose first content word matched counts, because a later word would add the time of the words before it.
A window needs 3 such cues. A window with fewer gets a window of 24 seconds around it, heard in one more hearing, and
the longer window stands for it. The windows that decide the fix must lie in both halves of the file, because a cut
in a half with no window goes unseen.
Right tracks start their cues close to the first heard word, some a little before and some a little after. A fix keeps
the median lead of right tracks, so a fixed copy lands within the spread of those leads, a few tenths of a second.

*Roll-up captions* are closed captions that show each new line under the lines before it. Each cue repeats the lines
above its new line, so its first words were spoken two or three lines earlier, 2 to 4 seconds before the cue shows. A
cue timed by them reads seconds late, by a different time each cue. So every timing check reads a cue of a roll-up track
by its new lines only, less a speaker's name such as `>> Reporter:` (`subsync.spoken()`). That covers the word check
and its spans test, the line windows, and the anchors of the whole-file timing. A track counts as roll-up when half
its cues or more repeat lines of the cue before (`ROLLUP`). Roll-up tracks sat at 69% or more, other tracks at 30% or
less. The most among them were paint-on captions, which show each line as it is typed. Other tracks keep their text,
because a line said twice in a row there was spoken twice. A roll-up track that is off still reads off by its new lines,
and gets its fix.

- The track is in time when the cues at the file's start and end, and at every window, sit under 0.75 seconds off. So
  the drift is judged by the error it causes at the file's ends, where it is largest. Right tracks sat closer, and a
  24/23.976 drift of an 18-minute episode sat farther. A plain shift under 0.75 seconds is in time too. A right track
  can trend a few tenths of a second over the file, and a line through two of its windows then runs past 0.75 seconds
  at a file's end. A shift would only move the rest of the track off.
- A ratio of 1, 25/23.976, 25/24 or 24/23.976, or the inverse of one, fits when every window, a middle one too, lies
  within 0.3 seconds of one offset at that ratio. The middle window must also lie within 0.3 seconds of the line through
  the early and the late window, and that line must stay under 0.75 seconds from the offset at the file's ends. The
  windows of a right track differ a little, and a line through them would move that noise out to the ends. The ratio
  with the least error at the file's ends wins, and a plain offset wins a near tie.
- A ratio other than 1 needs a middle window, heard only then. Two windows cannot tell a drift from a cut between
  them. A cut of 30 seconds can look like 25/24, and a cut of a second can look like 24/23.976 in an episode. The
  middle window lies from 40 to 60 percent of the way from the early to the late window. So a cut anywhere between
  them puts it at least 0.4 of the cut off their line. A cut of a second then puts it 0.4 to 0.6 seconds off, over the
  0.3 seconds a fix allows. At a third of the way it sits only a third of a second off, and the spread of a right
  track can hide that. Only a window in that part counts as the middle one, so windows that a drift hearing adds near
  the ends never confirm a ratio. The window takes the densest cues there, and the fix to confirm says where their
  speech is in the audio, many seconds away for a 25 fps track. When it hears under 8 words, as on a chant that
  Whisper drops, a window of 24 seconds elsewhere in that part is heard in the same process. When both hear too
  little, the track keeps its times with no alert.
- Two windows can also land on a short patch of a right track that sits early or late. So on an import, and in a
  `--sub-check` backfill, a small fix needs one more hearing. A small fix moves the cues under 1.5 seconds at both
  ends of the file. A fix that moves them more, as a frame-rate drift does, keeps the rules above. The hearing takes a
  window at about a third and one at about two thirds of the file, away from the windows heard already. A window that
  hears too little gets a window of 24 seconds elsewhere in its part. Each of them must hold 3 matched cues and sit
  within 0.3 seconds of the fix's line. A window too thin to judge confirms nothing. These windows never change the
  verdict, and the hearing costs one more run of the model with two windows. A small fix they do not confirm is not
  applied. The times stay with the fix's offset in `unfixed`, so they alert. An import with the deep analysis on holds
  that alert, and the deep analysis judges the track. A check after a conversion picks the same windows again.
- `--sub-time` and the deep analysis hear no such windows. The whole-file timing hears the whole audio there and times
  every line, see "Whole-file timing" below. It takes the place of the word check's fix, so that fix never moves a
  line. The track's timing then holds the whole-file timing's own why, and the word check's why stays in
  `word_check`. A track the whole-file timing does not judge, as when it anchored too few lines, keeps what the word
  check found. Its fix moves nothing and stays in `unconfirmed`, and `unheard` marks it.

  A word-check window is unsure when its heard words sit under 0.75 seconds off, its anchors sit 0.75 seconds or more
  off, and the two lie 0.8 seconds or more apart, see `subsync.unsure_window()`. Two cues shown a second early over a
  sound move the cue starts, and the words still sit in time. The steps stand, and "unsure" holds the fit of the other
  windows for the decision log. With `SUBTITLES=deep` the import holds the alert, and the whole-file timing of the deep
  analysis judges every line.
- No ratio that fits is two offsets, a different cut, and the times stay. A window 0.75 seconds or more off posts, as
  heard evidence does, see "Timing outcome". Steps whose windows each sit under that go only to the decision log and
  the backfill report.
  Two ratios that fit and move the cues apart by more than 0.3 seconds at the file's ends give no fix and alert, a
  plain offset among them. Two windows cannot tell a plain offset from 24/23.976 when their offsets differ by half a
  second.
- After the fix, 80 percent of the matched cues must fall inside their spans, and more of them than as they are
  (`subsync.shares()`). A tie keeps the times, and the offset alerts as one the word check could not fix. Without the
  deep analysis nothing judges that alert again, so it posts even for a right track. The rule counts the matched cues
  of the word check's own windows. On a right track, the word check's line ran 0.757 seconds off at the file's start,
  7 ms past 0.75 seconds, and 1001/1000 fit its three windows. That fix moved 107 lines in time out of half a second.
  Windows heard once a minute put 96% of the matched cues in their spans as they were, and 83% after the fix. On
  a real drift the share rose from 66% to 90%. Known limit: a cue that shows for its whole line keeps its heard words
  inside its span through a move of about a second. A smaller fix of such cues cannot raise the share, so they keep
  their times. `--sub-time` and the deep analysis skip this rule, since the whole-file timing takes the fix's place.

A rule that every matched cue agrees within 0.3 seconds fails on right tracks. Right tracks start their cues 0.9
seconds early to 1.5 seconds late against the speech, so the rule takes the window medians and 80 percent of the cues.

**Actions.**

- A Matroska track that does not match leaves the file in a remux (`subtitle_mismatch_removed`), and a "Wrong subtitles"
  alert goes out. A track whose times need a fix gets them in the same remux (`subtitle_retimed`), so a file that needs
  both is remuxed once. So do the moves of the whole-file timing (`subtitle_blocks_retimed`), see "Whole-file
  timing". The remux runs under the exclusive lock, with the temp file in `.<NAME>-convert`. Its work files, the
  extracted text, the proof's reads and the cover attachments, go into a folder under `STATE_DIR`, never the system
  temp dir, which is often a small tmpfs. The trim, the damage read, the conversion and the video windows keep their
  work files there too. A worker that starts removes a work folder that a killed step left over a day ago. ffmpeg
  copies every
  kept stream with `-copyinkf`, and reads a retimed track from a second input of the same file with `-itsoffset` and
  `-itsscale`. A cue that the fix moves before 0 starts at 0, through the `setts` filter. A cue that ends before 0
  keeps a length of 1 ms, so it never shows. With a length of 0 the proof refused the remux, as when a recap of
  several cues moves before the start. A negative time would make ffmpeg move every stream. When the audio has a
  codec delay, as AAC, Opus, AC-3 or MP3 that ffmpeg wrote into Matroska has, that start at 0 moves the later cues of
  the track by the delay. The proof then refuses the remux, the file stays as it was, and the "subtiming" alert says
  the remux failed. A fix that moves no cue before 0 passes. mkvmerge moved AAC lace times by 2 ms in a Matroska to
  Matroska remux, and the proof refused it, while ffmpeg keeps the times it reads. A line that ffmpeg prints fails
  the remux, with one exception. ffmpeg decodes a few frames of each stream while it opens the file, and the decoder
  of a damaged frame prints there, as in `[eac3 @ 0x...] error decoding the audio block`. The copy decodes nothing,
  so a line whose name is the codec of an audio or video stream of the file passes. The log keeps it in the remux's
  `warnings`. A muxer line, a filter line and a plain error still fail.
- mkvpropedit then puts back the Segment UID, and each kept track's UID, BCP 47 tag, name and flags. ffmpeg reads an
  image attachment, such as a cover, as a picture stream and would write it as a video track. So the map leaves it
  out, and mkvpropedit adds it back with its name, type and UID. The new file must
  keep the type, codec and language of each kept track in order, its UID, its BCP 47 tag, the properties in
  `KEEP_PROPS` (its name, flags and codec private data), and the count of attachments and chapters. The codec private
  data of HEVC video is its hvcC record. It holds the parameter sets and other units a decoder reads before the first
  frame, one array per unit type. ffmpeg 7.1 writes it again with the array_completeness bit of each array cleared. A
  1 says the stream holds no more units of that type, and a 0 says it may. So an HEVC header also passes as the old
  one with those bits cleared. Any other change of a byte fails. ffmpeg also writes the track's colour elements from
  the video stream, sets MaxBlockAdditionID to 0 and drops BitsPerChannel. `KEEP_PROPS` holds none of them, so they
  pass. The proof of "Conversion" must pass with the retimed
  tracks' times as the fix moves them. It also holds each stream to its own start time, so a remux that moved every
  stream fails. The file keeps its owner and mode, and the original is kept as in "Kept originals".
- A removal cannot be undone without the kept original. So with `KEEP_ORIGINALS_DAYS` 0, or when the remux or the
  proof fails, the track stays in the file. It gets the role `unmatched` instead, which no policy lists, so it never
  becomes a default. It loses its default and forced flags with mkvpropedit (`subtitle_audio_mismatch`), and the alert
  says why it stayed. A retime of another track in the same file keeps that. `SUBTITLES=check` keeps the times, the
  tracks, the sidecars and the flags, and alerts.
- A sidecar beside a Matroska file that does not match moves into `.<NAME>-originals` as a kept original, with a log line and
  the alert. A sidecar whose times need a fix is written again with new times, and its original is kept the same way.
  Only its time lines change. Every other byte stays, so its text keeps its character set, even one the detection
  misses (`subtitles.raw_srt()`). With `KEEP_ORIGINALS_DAYS` 0 the sidecar stays as it is and alerts. The program
  that wrote it, such as Bazarr, can download the same wrong file again, and the hook does not tell it. A later check
  moves it again.
- In a conversion, a sidecar that does not match is not muxed. It moves into `.<NAME>-originals`, and with
  `KEEP_ORIGINALS_DAYS` 0 it stays beside the file. A built-in track that does not match is left out of the remux, and
  the proof leaves it out too. The conversion then keeps the original in `.<NAME>-originals`, as a forced conversion does, and
  the alert names it. With `KEEP_ORIGINALS_DAYS` 0, or no place to keep it, the track stays in, and the check of the new
  file turns its flags off. A sidecar whose times need a fix is muxed with new times, and its original is kept. A
  built-in track whose times need a fix gets them after the conversion. The check of the new file reads the same
  windows and the words carried over, so it hears nothing again.

**Cost.** The target is 15 CPU seconds per file with a track to check, the model load included, and nothing for a
file without one. A third window costs one more Whisper run in the same process, only for a file whose window heard
too little. A middle window costs one more hearing, only for a track that needs a ratio fix, and its window of 24
seconds one more Whisper run in that hearing. A drift hearing, the hearing at a matched window's offset and a longer
window at too few cues cost one more hearing each, only when the check asks for them. A check makes at most five
hearings after the first, so a ratio from the hearing at an offset still gets its middle window. A hearing starts only
when 10 seconds of the check's time are left. More threads cost more CPU time than they save in wall time, so the
check runs one thread. A failure or a timeout gives unknown and never stops the import. The check shares the job's
time limit, 90 seconds at most.

**Cache and backfill.** `lid.sqlite` also holds each file's saved result. It holds the version and the findings of each
check the run made, the app, the run, and the size and mtime of its sidecars. It has a mark when it asks for an action
that no apply made yet. A backfill with `--sub-check` skips a file whose result is saved for its size and mtime and its
sidecars, comes from a run that makes every check of `--sub-check`, has no stale check, and asks for nothing more. So a
stopped run goes on where it stopped, an apply after a dry run still acts, and a new sidecar from a program such as
Bazarr is checked. `--sub-check` also takes a file with a sidecar that the flag backfill leaves out, and `--recheck`
skips no file. The decision log holds each result in `subcheck`, the remux in `subremux` and the sidecar actions in
`sidecars`.

A check is stale when its registry entry says a newer version can fix its findings, see
[development.md](development.md#when-a-change-can-fix-old-files). After an update, `runner.queue_rechecks()` queues a
recheck job of each stale file in the background queue, behind the imports and the deep analyses. A recheck makes the
checks of the run that saved the result, and none deeper.

**Flash cues.** A release can store right starts with ends a tenth of a second later, so a player flashes each line.
The check reads the CueDuration of each subtitle entry from the Cues, which mkvmerge and ffmpeg write, so it reads no
block for this. A text track or `.srt` sidecar whose median cue shows under 0.5 seconds, in any language and role,
forced too, flashes. Only a cue with a visible character counts, so a cue of tags or of a zero-width space is left
out, and the track needs 20 such cues. Half its short cues must also end over two frames before the next cue starts.
A sign drawn frame by frame, or a karaoke line, runs straight into its next event, so it never flashes. Right text
tracks show their median cue for a second or more, and a flash track for about a seventh of a second. Only a cue under
0.5 seconds gets a new end: the next cue's start less two frames (0.083 s), at most the start plus twice the reading
time, 3 seconds at least and 7 at most. The reading time counts the visible characters, 17 a second. A flash fix never
shortens a cue. A built-in track gets its new ends in the remux of "Actions": mkvextract writes its text, the ends go
in, and mkvmerge reads it back, which keeps each packet and the ASS header byte for byte. HandBrake ends each ASS event
and the ASS header with a NUL byte, and writes no `[Events]` section into the header. The round trip drops those NUL
bytes and adds the section. The proof compares each packet that ended in a NUL without that one byte, and names their
count in `nul_cut`. The header must be the old one less its NUL, then the `[Events]` section and its Format line.
mkvextract writes each line break inside a SubRip cue as LF, and mkvmerge may store it as LF or CRLF. mkvmerge 92 stores
CRLF, and a newer mkvmerge may store LF. So the proof compares each packet of a SubRip track that went through the round
trip with each CRLF as LF, in both files, and names the count of packets that changed in `crlf`. A track that ffmpeg
copies from the source file keeps the strict rule, also when `-itsoffset` moves it. Any other change of a byte fails.
The proof then holds each end to the plan. ASS times are centiseconds, and HandBrake writes starts in milliseconds. The
round trip puts each start on the centisecond grid, up to 5 ms away. So such a track gets one plan of every start and
end, with each start on the nearest centisecond, and the proof holds both to it. The log counts in `starts_rounded` the
starts the grid moved off the time of the fix. A sidecar is written again, and its original is kept. A fix goes to the
log only (`subtitle_ends_lengthened`), and a failed one alerts. `SUBTITLES=check` keeps the ends and alerts. A WebVTT
track that flashes is reported and never rewritten. Imports, `--sub-check`, `--sub-time` and the deep analysis run it.

**Reference timing.** A text track in another language than the audio, Japanese, Chinese or Thai text, a PGS or VobSub
track, and an `.srt` sidecar the word check leaves out get their times from a reference. A reference is a track or
sidecar of the same file whose words matched and whose times are in time or fixed, with its fix applied. Forced and
commentary tracks stay out. The fit reads no words. It compares when the cues show, in the manner of alass, so a
translation that splits or merges lines still fits. It tries each ratio at the offset the cue span starts point to, and
keeps the one where the cues cover the most of the reference's, each cue counted up to 10 seconds. The score is that
overlap above its mean at ten offsets far from the best, so dense cues do not score by chance. A span of cues that
overlap votes once, so the thousands of short events of a karaoke song or an ASS drawing outvote no dialogue. A right
pair scores well over 0.3, and another episode's track or another film's scores near 0. A research prototype of lapse's
peak sigma scored another episode's track almost as high as a right one, so the fit keeps this score. Under 0.3 is a
weak fit, which only reports, and incorrect subtitle identification then judges the track, see "Incorrect subtitle
identification". A fit then pairs cue starts with reference starts in ten slices of the span, each at its own offset,
and the rules of "Timing" judge the slices. Every slice must lie within 0.3 seconds of the fix, and a ratio needs a
middle slice on its line. So a cut, in one step or in several, gives no fix and an alert, and the alert says the track
disagrees with its reference. The alert posts when the slices differ by 1 second or more, or when two slices or more
share an offset at least 0.5 seconds from the rest (`subsync.stepped()`), because no jump fix moves such a track. It
names the least and the most the slices sit off, so a drift that no ratio fits reads right too. Five slices fixed more
shifted copies of real tracks, but they let a cut in steps pass several times as often, so the fit keeps ten.

- **Sparse slices.** A slice where the track or the reference starts under 6 cues is left out, such as the credits or
  a song only one side times. Chance pairs of so few starts can outvote the right offset. Each half of the span needs 3
  slices that are not left out, else no fix.
- **The slice search.** A slice searches its offset within 60 seconds of the whole track's offset, so a cut of up to a
  minute shows. A search over the whole 120 seconds let chance put a slice of a right track far off, which read as a
  cut. A peak more than 1 second from the track's offset must hold over 1.5 times the pairs of any other offset, else
  the slice has no clear offset. A peak at the track's offset needs no such margin, because a right slice often holds
  a second peak one line away.
- **A slice with no clear offset.** A slice that holds a cut pairs some lines at the offset before the cut and some at
  the offset after it, so it often has no clear offset. It is left out, and the times stay. The other slices still
  alert when they show the cut. When they all agree on one offset 0.75 seconds or more off, the alert names that
  offset. When they do not alert, the left-out slices can. Each one's own peak needs 3 pairs. When 2 or more of those
  peaks lie within 0.3 seconds of one offset, 0.75 seconds or more off, that offset joins the offsets of the other
  slices (`subsync.WEAK`). So the alert names the parts that sit off, as for a cut the clear slices show. No fix takes
  it. One such slice alone can be chance, or a scene one track leaves out.

The reference is in audio time. A fixed reference moves by its fix. An in-time reference moves by its own measured
offset, which can reach 0.75 seconds. So a target's offset to the audio is its offset to the reference plus the
reference's offset, and a fix comes only when that total is 0.75 seconds or more. In `--sub-time` and the deep
analysis the whole-file timing judges a matched track in place of the word check. A track it judged with no stretch
still off after its moves is a reference, with each line where the moves put it. A track with a stretch still off is
none, because the slices of a right target would disagree with it. The fix moves the target to the audio. When a second reference also fits the target, the fix must give the same times against it at both ends of the
file, else the times stay. A picture cue is a PGS display set with an object up to the next set, or a VobSub packet up
to its stop command, and it ends 10 seconds after it starts at most. A PGS block is one small read of its first bytes.
The fixes go into the one remux with the word check's. ffmpeg's copy of a PGS or VobSub track passes the proof, and a
ratio also scales each cue's duration. With no reference, nothing is read.

**Two stages.** An import, `--sub-check` and `--sub-time` run the reference timing. An import reads and fits one track at
a time, and each read and each fit starts only while its time limit leaves 150 seconds. A track it has no time for is
`deferred`, and so is every track after it. The flash check reads its tracks the same way. So a deadline never ends an
import in an error, and the flag edit still runs. An import never reads a whole file, so a track the Cues do not index
is skipped, and the log says so under `unindexed`. With `SUBTITLES=deep`
an import with a subtitle track or sidecar then queues a deep analysis of its file, see "How it runs". The deep
analysis runs the check of `--sub-time` with the edits, the proof, the kept originals and the Plex analyze of an import,
and it posts only its subtitle alerts. `--sub-check` adds the whole-file read and never hears the whole file, because
that hearing of a library would take weeks.

- **The whole-file read.** Old mkvmerge versions wrote no cue entry for a subtitle block, and many files in a library
  come from them. For those tracks `--sub-check`, `--sub-time` and the deep analysis read the whole file once, with one
  ffmpeg at nice 19 and idle I/O that writes each track in its own format into its own pipe. The read keeps only the
  cue times and text as they arrive. A PGS track keeps only its PCS segments, because its pictures can take GBs. So
  the read writes no file and needs no free space. `--sub-check` and `--sub-time` read under the shared file lock.
  The deep analysis reads before it takes the lock, since a film takes minutes. The read is kept by the file's size
  and mtime, so a file that changed meanwhile is read again under the lock. A deep analysis that yields keeps the
  read in the store for its next run, and a track whose read was cut short stays marked as cut there. ffmpeg reads
  mkvmerge's WebVTT codec id as unknown, so such a track stays unread.
- **Whole-file hearing.** `subtitles.whole_heard()` gives the whole-file timing the evidence of a whole audio track.
  That is the words Whisper hears, the spans of speech and the speech onsets. It hears windows of 10 seconds on a fixed
  grid, one every 7.5 seconds from the file's start (`subsync.whole_grid()`). The last window ends at the file's end.
  Two windows go through Whisper in one run. Each second of the audio then lies 1.25 seconds or more inside a window.
  Two windows that overlap split their shared audio at its middle, so each word comes from one window
  (`align.heard()`). The cache keeps each pair of windows as Whisper hears it. A window of the grid that the cache
  holds already is not heard again (`subsync.whole_starts()`, `lid.windows_get()`). A window off the grid never
  counts, such as one of the word check. So a run that stopped goes on where it stopped, and a second run hears
  nothing. A cached row that does not read drops alone, and the facts name it. A 22-minute episode hears about 180
  windows. The spans of speech come from one read of the whole track, and the onsets from reads of 10 minutes each.
  The deep analysis yields to an import between two pairs, during the read of the speech and between two onset reads.

  The hearing runs on the CPU of the container, at about 1.9 CPU seconds a window. That is about 15 CPU minutes an
  hour of video, about 5.5 for a 22-minute episode and about 30 for a 2-hour film. The deep analysis of 2.7.0 spent
  about 75 CPU seconds on an episode in time.
- **Whole-file timing.** `subtitles.sub_whole()` times every line of each subtitle the word check matched. It hears
  each audio track once, in the language the word check heard it in. `align.run()` then times each subtitle on those
  words in the subtitle's own language, whose stopwords never align. It takes the lines in start order, in five steps.

  1. *Anchors* (`align.anchors()`). One alignment, in order, pairs the content words of every line with the content
     words heard in the whole file. Only runs of 2 pairs or more count. A line anchors at the heard time of its first
     content word. The anchor then steps back over the line's words before it, such as "so" or "the". It steps back while
     each heard word before it is that word, under 1 second before the next. There is no search for an offset, so any
     offset, drift or jump aligns. A roll-up line counts by its new line, and a speaker's name is never spoken. Three
     kinds of line never anchor. A line shown under 0.05 seconds once paired with its neighbour's speech 3 seconds off.
     A line of song lyrics never anchors, and neither do two lines in a row with the same content words.
  2. *The curve* (`align.curve()`). A Viterbi fit puts one offset curve through the anchors. The curve is a chain of
     segments. In each, a line's start is its speech times a frame-rate ratio, plus a level. The fit tries every ratio
     of `subsync.RATES`, with levels 0.02 seconds apart. Each anchor costs its distance from its segment's line, 1
     second at most, so an outlier costs no more than a second. A change of segment costs 5 seconds, and a ratio other
     than 1 costs 2 more to enter. So only a real jump or drift starts a segment. A segment's level is the median of its
     anchors off its ratio. The first pass fits the file's offset, drift and jumps. A second pass fits ratio 1, at a
     change cost of 1 second, to the starts the first pass gives. It finds local blocks against them. The moves of the
     two passes add up.
  3. *The gate* (`align.plan()`, `align.gate()`). A segment moves when its first or last anchored line sits 0.5 seconds
     or more off its speech, and its evidence passes one rule.
     - A segment of 30 anchors or more moves on Whisper alone, at any distance.
     - A smaller segment never moves when the speech onsets disagree, or when it sits over 10 seconds off. Lines that
       far off their neighbours are more likely lines the audio does not hold, as a recap or another cut.
     - It moves when the onsets agree.
     - On Whisper alone, it moves with 6 anchors at a median of 1 second or more off, or with 15 anchors at 0.75
       seconds. It also moves with 3 anchors 2 seconds or more off that lie within 0.15 seconds of its line. A jump
       near a file end leaves such anchors. An author's scatter rarely puts 3 lines that far off together.

     A segment moves its anchored lines. It also moves each line with no anchor whose anchored neighbours both lie in
     it within 15 seconds of the line (`align.owners()`). In a segment of 30 anchors or more the distance does not
     count. A file edge counts as a near neighbour.

     The onsets are a second clock that does not use Whisper. `subtitles.onsets()` reads them with ffmpeg's
     silencedetect at -25 dB, where a silence of 0.3 seconds or more ends. It reads the centre channel when the track
     has one, because the dialogue is there, and else the mono mix. Only an onset after a silence of 0.5 seconds or
     more counts. The offset between the two clocks is the median of onset less anchor (`align.onset_lead()`). It takes
     the anchored lines with exactly one such onset within 1 second, and needs 10 of them. A line is timed at a place
     when an onset lies within 0.15 seconds of where its speech starts there. Only lines that move over 0.3 seconds
     count. The onsets agree when 20 percent of the segment's lines, and 3 at least, are timed where the move puts
     them. That count must be twice the count where they sit, and arise by chance with a probability of 0.001 at most.
     The chance comes from the same count 7 to 53 seconds from those places (`align.vote()`). The onsets disagree
     when as many lines are timed where they sit, and enough to judge.
  4. *The screen* (`align.short_of_speech()`, `align.apply_order()`, `align.ends()`). An anchored line moves toward its
     own speech and never past it. A line with no anchor then moves the smaller of these moves of the anchored lines on
     each side at most, and stays when one of them stays or moves the other way. No line crosses another. Two line starts stay 0.5 seconds apart after the moves, or
     as far apart as they were. The line that moved toward the other yields, and never past its old start. A line cut
     more than 1 second short of its move stays, so later lines never pile up behind it. A move that would start a
     line before the file's start drops. A moved line keeps its length. It stops two frames, 0.083 seconds, before the
     next new start, and shows 0.5 seconds at least, unless the next line starts sooner. The next line is the first
     later line it did not show over (`align.after()`), so lines shown inside a long line do not count. A line that ended under 0.1
     seconds before the next one, or ran 0.02 seconds into it, keeps that small gap. It runs up to the next new start
     less the gap, for 7 seconds at most, or for its old length when that is longer. Only a line with no line shown
     inside it grows that way. A long line with lines inside it stops two frames before its next line. So a move opens
     no blank and stacks no lines.
  5. *Live captions* (`align.per_line()`). The scatter is the median distance of the anchors from the first pass's
     curve. At 0.6 seconds or more the subtitle is live captions, typed some seconds after the speech by a different
     time each line. Each anchored line then moves onto its own speech. A line with no anchor moves by the smaller move
     of the anchored lines on each side, when both move the same way, else it stays. Lines that would cross are cut
     short instead of staying, as one line that stays would hold back every later line.

  A track needs 10 anchored lines before any line moves. `align.judged()` then fits the curve again at the new starts,
  at ratio 1, and names the stretches still off, see "Timing outcome". It judges nothing under 20 anchored lines.

  The decision log keeps the timing of each subtitle in `whole`. It holds the segments of both passes, each with the
  rule that decided it. It holds the scatter, the moves by each line's place in start order, and the judge. It also
  holds the count of anchored lines, and the stretches off before any move in `before`. `lag` is the median lag of the
  anchored lines, and `heard` the audio the windows hold. `stopped` says why the hearing stopped, when it did.
  `whole_facts` holds the cost of each hearing. `remux.time_plan()` takes the new start of each moved line by its place
  in start order. `align.ends()` gives the ends, from the flash ends when there are any. A built-in SubRip, ASS or SSA
  track gets one plan of new times for every line, with its flash ends. `resub()` writes it through the text round
  trip of the flash fix, with no `-itsoffset` (`subtitle_blocks_retimed`). The proof holds each start and end to the
  plan. A sidecar is written
  again with the plan when its text gives the lines the word check read. A WebVTT track is never rewritten, so its
  moves only report.

  A hearing that stops part way, as a Whisper run that fails, judges nothing and moves nothing. The outcome is unknown,
  so a held import alert posts, and the result stays pending. Nothing queues a new run. A later `--sub-time` or deep
  analysis of the file goes on where the hearing stopped. A track
  whose read was cut short gets no whole-file timing. With `AMG_INVARIANTS=1`, `align.check_plan()` checks every move,
  see [development.md](development.md#safety-self-checks).

  The owner picked this method from a study of real and planted subtitles, scored against the speech a larger model
  heard. The study set the values above. Known limits:
  - A line with no anchor moves no farther than the anchored lines around it. So one anchored line that stays holds
    its unheard neighbours back, even when the segment around them is off.
  - A stretch of lines with no heard words right before a jump can move with the part after the jump. That happens
    when one line in the stretch paired with words heard far away in the direction of the move.
  - Whisper's word times can run late over a long stretch. With no speech onsets to check them, the timing reads that
    as the subtitle off, and moves it.
  - A stretch of 3 to 5 anchored lines off by under 2 seconds, or of 6 to 9 anchored lines off by under 1 second,
    posts nothing. It moves only when the onsets agree.

**--sub-time.** `arr-media-guard --sub-time PATH [PATH ...] [--apply]` runs all of it on each named file, past the cache,
and writes the cache. It acts as a backfill does, with the file lock, `.<NAME>-convert`, `.<NAME>-originals`, the proof and a Plex
analyze after an edit. Its alerts print and never post. An app names the item only for the original language and the
inputs of the check: one list call per app, and the episode files of the one series that holds the path. A path no app
lists, such as a copy, runs with no original language, so the decision cannot be trusted. It keeps every flag then, as
the deep analysis does, and only a subtitle verdict changes a flag. A user who never set up an app gets no line for
it. Such an app has no API key, no `config.xml`, and no URL or the URL that the env files ship. The image writes both
env files into a new `/config`, so a one-off container has both shipped URLs. When neither app is set up, one line says
so for every path. A lookup that fails, by an error, a timeout or an app restart, is not the same: `--apply` then stops
before any change, and a dry run says why it knows no original language. An app with a URL of the user's own and no
API key that reads is set up in part, and it fails the same way. A backfill or a scan of an app that is not set up
stops with one line that names the settings. So do the audit's pruning and the subtitle hunter.

A dry run says each subtitle alert as `--apply` would act on it, in its decision line and its CLI line. The check
records what `--apply` would do with the remux. `subtitles.remux_block()` then asks `remux.repack_block()` again for the
reason of a skip. Its code is `remux` for a remux that would run. Else the code names the reason of the skip, one of
`hardlinked`, `cap`, `space`, `keep_root`, `keep_create` and `keep`. `report.BLOCKS` words each code. A file with another hard link stays as it
is, flags included. A keep folder that is not writable names the folder, the uid and the gid of the run, which `PUID`
and `PGID` set in Docker. When the folder does not exist, creating it is enough. An import and `--apply` say what they
did.

**Centre channel.** Whisper hears the mono mix of all channels. The centre channel alone kept the language verdicts,
but a subtitle check lost its match where the centre channel was quiet and the mix still held the words. The mix
stays.

## Timing outcome

The subtitle checks hear evidence and propose moves. The planner picks the moves (`process.subtitle_checks()`), and the
remux or the sidecar rewrite writes them. Then the timing judge, `judge.outcomes()`, gives each subtitle one outcome
from its evidence, and the alert words only that outcome (`judge.alert_lines()`). No other step decides whether a
subtitle's times post. The decision log keeps each outcome in `subjudge`.

The judge takes each piece of evidence to where the plan puts its lines. A plan takes effect when the run wrote it,
or a dry run planned it. A write that failed, or that `SUBTITLES=check` left out, moves nothing. Two kinds of heard
evidence give the stretches still off.

- **The whole-file timing** of `--sub-time` and the deep analysis judges every line it anchored, see "Whole-file
  timing". When its moves took effect, it reads the curve fitted again at the new starts, and else the lines where they
  sit. A stretch still off is a segment of 10 anchors or more that sits 0.75 seconds or more off. Shorter runs posted
  the scene lag of real tracks in time. So is a segment of 3 anchors or more that sits 2 seconds or more off, with its
  anchors within 0.4 seconds of its line. A post edits nothing, so it takes looser evidence than a move. A stretch runs
  from its earliest line to its latest, and sits off by its segment's level. It counts every line between its first
  and last anchored line. A stretch that starts at the first anchored line starts at the subtitle's first line, because no
  line before it was heard in time. One that ends at the last anchored line runs to the subtitle's last line the same
  way (`judge.whole_stretches()`). When the timing judged nothing, the word check's own finding posts as a stretch of
  the whole file. That is a fix no check confirmed, an offset no fix lined up, or steps.
- **A word-check window** 0.75 seconds or more off is a stretch still off. An import has only these.

Live captions keep the stretches of the whole-file timing, and their alert says how many lines moved and how many
stay off (`judge.live_facts()`). A subtitle timed by a reference or by the speech layout keeps the rules of its fit.
Slices that no ratio explains post when they sit 1 second or more apart, or when `subsync.stepped()` shows a jump. The
speech layout drops the mark of such slices whenever `subsync.stepped()` shows no jump, as when one slice alone sits
off at an end song. That subtitle then posts nothing. An offset no fix lined up posts. The thresholds sit in one table,
`subsync.TIMING`.

An outcome is one of five states. In time: nothing moved, and nothing sits off. Fixed: lines moved, and nothing sits
off after the moves. Partly fixed: lines moved, and stretches still sit off. Off: nothing moved, and stretches sit off.
Unknown: no evidence judged the times. Partly fixed and off post, and the others post nothing. A partly fixed alert
says what moved and where lines are still off, and never that the lines were left as they are. It names each
stretch from where to where, as "12 lines from 3:10 to 4:05". A stretch that runs to the last line reads "from 3:10
on", and one that starts at the first line reads "from the start to 4:05". So the count and the times name the same
lines, the unheard lines at a file end included. One stretch that holds every line reads as the whole subtitle off,
never as "in parts". With `DISCORD_POSTS=all`, the count of moved lines is in the alert only. A failed write posts
only its failure. A held import alert follows the outcome of the deep analysis, see "Alerts". It keeps the places its
outcome left off, and the deep analysis drops it only after it heard each of them again (`judge.heard_again()`).

`judge.check_outcome()` checks the outcomes under `AMG_INVARIANTS=1`, see [development.md](development.md#safety-self-checks).
Known limits: lines with no heard words, such as a song, give the whole-file timing nothing to anchor. So a short
stretch off among them posts nothing. A track only the speech layout timed has no cue starts, so the edges of its moves
never alert.

## Incorrect subtitle identification

A subtitle in a language that no main audio track speaks has no words to compare with the audio. The reference timing
of "Subtitle match" fits it to a track whose words matched, and a weak fit only reports. A file with no such track
gives the subtitle no check at all, such as an English subtitle on a Japanese film. Incorrect subtitle identification
checks such a subtitle by where its lines show, with the speech layout check. It reads no words, so it works in any
language. It alerts on a subtitle of another episode or version, and on one that is off by one shift.

**The idea.** A subtitle shows its lines while people speak. Silero VAD (voice activity detection), which
faster-whisper bundles, marks the spans of the audio that hold speech. A right subtitle shows its lines over those
spans, at one offset and one frame-rate ratio. Another episode's subtitle has its own rhythm of lines and pauses, so
no offset lines it up.

**Which subtitles.** A text track in a full, SDH or dub role, and an `.srt` sidecar that is not forced, whose language
no main audio track speaks. The reference timing must have found no fit for it: it had no reference, or the fit was
weak. A subtitle that fits a reference needs no more check, so its audio is never read for it. The check runs in
`--sub-check`, `--sub-time` and the deep analysis. An import never runs it, because an import never reads the whole
audio. With `SUBTITLES=deep`, an import with a subtitle queues the deep analysis, and that runs it.

**The read.** `lid.speech()` reads the whole audio track that plays, once per file, with one ffmpeg at nice 19 and idle
I/O, mono at 16 kHz. Silero VAD gives a speech probability for each frame of 32 ms, 10 minutes of audio at a time, so a
film never sits in memory whole. A span of speech starts at a probability of 0.5 and ends under 0.35, Silero's own
thresholds (`subsync.voiced()`). Spans less than 0.5 seconds apart join. The decode starts at the track's first packet,
so the spans move by the delay ffprobe gives the track (`lid.start_delay()`). They then run on the file's clock, as the
cues and the speech onsets do. Without it, the right subtitles of a file whose audio starts 1.5 seconds late seemed 1.6
seconds late and alerted. `lid.sqlite` caches the spans by path, size, mtime, stream, those settings and the
faster-whisper version, which ships the Silero model. An edit carries them to the new file. Silero VAD needs no Whisper model, so the read takes no turn at the model. It runs without the file lock, as
the hearing does. The deep analysis asks first whether an import waits, and again after each 10 minutes of audio. When
one waits, the read stops, caches nothing, and the job goes back to its queue.

**The fit.** `subsync.layout()` fits the cue spans to the speech spans as the reference timing fits a track, with one
change. Each ratio tries the three offsets that the starts of the cue spans point to most, and the fit with the most
lift over chance wins. A span of cues that overlap votes once. On verified right anime tracks, the thousands of short
karaoke and drawing events of an ending song outvoted the dialogue, and the right offset was never tried. The one peak
of the reference timing missed the right offset of some right tracks, because one span of speech can hold several lines.
So a whole-track offset of up to 5 minutes, or a frame-rate error, still fits, and the check needs no timing fix first.
The lift is the overlap at the fit, above its mean at ten offsets far from it, as a share of what was left above that
mean. Under 0.35 is a mismatch.

The check gives no verdict, and so changes nothing, when it cannot judge:

- The file runs under 15 minutes. A short file holds few lines, and chance fits score higher on it. In files cut
  short for a test, another episode's track lifted up to 0.71 at 5 minutes, 0.40 at 10 and 0.32 at 15, while right
  tracks stayed at 0.51 or more. The right tracks of a short file full of songs lifted as low as 0.43, near the bar.
- The track holds under 20 cues, or the audio holds under 60 seconds of speech.
- The lines show for under half the time people speak. Such a track does not follow the speech, as a track of sound
  captions and songs for a dub that the file does not carry.
- The lines cover 90 percent of the speech by chance, at any offset. Live captions that roll on with no gap do this,
  and their lift then tells nothing.

**Calibration.** The bar rests on real tracks that were each verified first. A right track ends within a few minutes of
the video, and its text reads as its tag. One track of its file passed a check against the speech. That is the word
check, or for a foreign film an English track confirmed by Whisper's English translation of two windows. Every other
track fits that one cue for cue at the same offset. A track that passed none of this stayed out. A wrong track is a
verified track against the audio of the next episode or of another show. Right tracks lifted 0.49 or more, copies
shifted by up to 45 seconds or timed for another frame rate too, and wrong tracks 0.27 at most. No right track fell
under the bar, and every wrong track the check judged did. Sidecars that fit no track of their file, made for a longer
cut or for another episode, lifted 0.25 at most.

**Actions.** A mismatch only alerts for now (`subsync.LAYOUT_ACTION`). The track or sidecar stays as it is, and the
"Wrong subtitles" alert says that the subtitles don't line up with the speech in the audio, so they may be from another
episode or version. A weak reference fit with a mismatch alerts the same way. The decision log holds each result under
`subtime` in `layout`, with its lift, and the read under `speech`. So a soak shows how far right tracks stay from the
bar. With `LAYOUT_ACTION` set to `remove`, a mismatch acts as one of the word check, see "Actions" in "Subtitle
match": the track leaves the file in the one remux, and a sidecar moves into `.<NAME>-originals`.

**Cost.** Decoding the audio costs most: about 0.7 CPU seconds a minute of audio for a 5.1 track, and Silero VAD about
0.25. A 45-minute episode costs about 40 CPU seconds, and a 2-hour film about 2 CPU minutes. The read reads the whole
file from disk. The fit costs under a second a track. A file whose subtitles all fit a reference, or that has none of
these subtitles, pays nothing.

## Foreign subtitle timing

Foreign subtitle timing retimes a subtitle in a language no audio speaks by its speech (`subsync.layout_fix()`). It
takes a track that incorrect subtitle identification fits but that runs late, or was timed for another frame rate.
`--sub-check` and `--sub-time` run it, and write with `--apply`, and so does the deep analysis. The code names it the
speech layout fix.

**The times.** A fit gives the offset and the frame-rate ratio, so a subtitle that fits but runs late gets a fix as in
"Subtitle match". The search reaches 5 minutes each way, wider than the 2 minutes of the reference timing. It found
verified right tracks shifted by 200 seconds, at 1.5 times the CPU, and no other episode's track lifted higher. The cue
spans fit the speech spans in ten slices, at the search's ratio and offset. Every slice must lie within 0.3 seconds of
one line, and a ratio needs a middle slice on it. The fix keeps the lines 0.2 seconds before the speech, the median of
verified right tracks. The moved lines must then fit again at ratio 1, within 0.3 seconds of that lead. A fix written
this way sat a median of 0.05 seconds from the right times.

The speech onsets are a second clock, as for the whole-file timing in "Subtitle match". They come from silencedetect, and the spans of
speech from VAD. One read takes the onsets of ten parts of up to 2 minutes, one in each tenth of the file, and only when
some subtitle has a fix. In each half of the file, at least 3 cue starts must have an onset where the fix puts them,
0.1 seconds after the start. That count must be twice the count where the cues sat, and rare by chance: a chance of
0.001 at most. A segment of the whole-file timing also needs 20 percent of its lines. A whole track does not
(`subsync.LAYOUT_ONSET_SHARE`), because under a music bed silencedetect marks few onsets. Verified real shifts of about
1 second drew onsets at 9 to 19 percent of their starts in a half, at a chance of 1 in a million or less, and the share
rule left them off. A half that confirms where the cues sat, or too few onsets, leaves the times as they are, and the
alert says the subtitles seem late. The onsets confirmed most fixes of shifted right tracks, and no fit of another
episode's track.

Slices at different offsets keep the times. When two slices or more share an offset at least 0.5 seconds from the rest,
the alert says the subtitles may be from another version (`subsync.stepped()`, `subsync.TIMING["move"]`), with the least and
the most the slices sit off (`subtitles.sub_findings()`). A mid-file jump leaves its slices that way, and so can a
drift that no ratio fits. On a real foreign track two groups of slices sat just under a second apart, and the old
edge of 1 second alerted nothing. One slice alone off alerts nothing: an end song that a few lines time did that on a
right track.

**The partial shift.** A step in the first or last tenth of the lines can pass every slice, because each slice holds a
tenth of the speech. A subtitle of a cut with a longer or shorter opening has a cold open in time and the rest shifted.
A fix of every line would then move the cold open off by the step. So a fix at ratio 1 moves only the lines that agree
with it (`subsync.shifted()`), and a run of lines at the start or the end of the file that has no evidence at the fix
keeps its times. A fix at another frame rate moves every line, see "Frame-rate fixes" below.

- Each line votes on whether it stays. A speech start within 0.3 seconds of where the fix puts the line votes to move
  it. A speech start where it sits, or none at the fix, votes to keep it. Each vote is the log of a ratio of two
  chances. On verified right tracks, 48 percent of the lines start near a speech start, and 10 percent at an offset by
  chance.
- The running sum of the votes from each end of the file peaks after the lines that stay. Every line up to the last
  place within 4.0 of the peak stays, so two chance speech starts at the fix near the end of a run that stays cut no
  right line off. With one start of slack, right lines with no speech under them and two chance starts in a row moved
  on a review's plants. The boundary comes from these votes, never from a share of the file.
- The first and the last span that move, the core's edges, need a speech start at the fix and none where they sit.
- The order of the lines holds. The core's own first or last line may move a line it passes, when the move goes toward
  that line's end of the file and passes it by 1 second or more. Then the line belongs to the moved part, as a moved
  line lands on its speech and passes no right line. A line that only such a passed line would pass, and any other
  pass, leaves the times as they are. On a review's plant a cold open in time, a short block 29 seconds late and the
  rest 35 seconds late, the move passed the block, the block then passed the cold open, and the cold open moved.
- A run that stays, of 6 lines or more, must not line up with the speech elsewhere, by a clear vote peak over 0.3
  seconds from where it sits. Else the times stay: the edge is off by its own offset.
- Every part of the lines that move must then line up with the speech in time (`subsync.edges()`). The first and the
  last 8 percent of them, at least 20, line up best there, and no part of 20 parts has a clear vote peak over 0.3
  seconds away. A run that stays keeps the rule above only: an opening song in time lines up with the speech nowhere,
  and the rule of the 8 percent read it as off. The speech onsets confirm only the lines that move.
- A track read only in part gets no fix, as the lines past the read were never judged, see "Long subtitle track
  handling".

**Frame-rate fixes.** A fix at a frame-rate ratio moves every line (`subsync.sat_on_speech()`). A frame-rate error
covers the whole file, so lines with no speech at either place move with the rest. Only an end that sits on speech now
stops it. The run from each end up to the peak of its stay votes must not hold 2 or more lines with a speech start
where they sit and none at the fix, at a chance of 0.05 or less. Such an end is in time while the rest is not, two
sources that one fix cannot serve. Then nothing moves, and the alert says the subtitles seem late. With the rules of
ratio 1 instead, real frame-rate tracks kept dozens of lines at their ends off, or wrote nothing.

**Limits.** A subtitle with one end in time and the rest at another frame rate moves the end off when no speech lies
under the end's lines, as under an opening song. On such planted subtitles about 7 in 10 writes moved right lines, a
few of them with speech under them. The library scan found no such subtitle. The owner accepted this risk.

No rule makes a right line safe for sure: a right line with no speech under it, beside three chance speech starts at
the fix, would move. On the plants of a review's shapes, of new strata and of real tracks, no right line moved. The cost is lines left: a run at an end with no evidence at the fix keeps its times. That holds the first
and last line or two of a track shifted as a whole, as no line of the move passes them when the shift is small.

**Actions.** A fix the speech onsets confirm writes the new times as in "Subtitle match" (`subsync.LAYOUT_FIX`). A
partial shift writes a plan of every line: its kept runs, in `keep` of `timing`, hold their times byte for byte. A
plan that would put a line with no text out of order writes nothing. A WebVTT track, which is never rewritten, gets no
partial shift and alerts. With `LAYOUT_FIX` set to `alert`, a fix only alerts. The track or sidecar keeps its times,
and the alert says how late the subtitles seem against the speech, and how many lines at the start or end would keep
their times. The decision log then holds the fix under `would` in `timing`.

**Cost.** A fix adds the onset read, about 1 CPU second a minute of the up to 20 minutes it reads. The votes and the
checks cost under a second a track.

## Status file

The hook records two checks in `STATE_DIR/status.json`, the TMDB key and the policy file. A
monitoring agent can read the file and alert on a failed check. Zabbix's `vfs.file.contents` item
with JSONPath preprocessing is one way. `health.py` writes the file.

```
{"version": 1, "last_hook_run": 1790000000,
 "checks": {"tmdb": {"status": "ok", "since": 1789990000, "checked": 1790000000, "error": "", "error_time": 0,
                     "last_24h": {"ok": 14, "unavailable": 0, "token_missing": 0, "token_rejected": 0}, "hours": {}},
            "policy": {"status": "ok", "since": 1789000000, "checked": 1790000000, "...": "..."}}}
```

The tmdb status is `ok`, `unavailable` or `token_rejected`. `token_missing` comes only from an older release, as
the code always has a key now. A file's `found` and
`no_record` both record `ok`, because TMDB answered. The policy status is
`ok` or `failed`. A check never recorded reads `unknown`. `since` is when the status last changed,
and `checked` is the last check. `error` keeps the last error text after a recovery, cut to 200
characters with anything that looks like a token replaced. `last_24h` counts each status over the
last day. `last_hook_run` is the last write by a job, a backfill, `--sub-time`, an audit or the subtitle hunter.

Each write goes through a temp file and a rename under a lock, with mode 0644. A broken file starts
fresh, and a failed write never stops a job. Give the state directory mode 0751 or wider, so the
agent can open the file by name. No file there holds a secret.

The policy status goes in at the start of each job and of each `--backfill`, `--sub-time`, `--audit` and
`arr-media-guard-subhunt` run. `--selftest` records it only with `ARR_MEDIA_GUARD_RECORD=1` in its environment,
and it never moves `last_hook_run`. So an install step can clear a policy problem, and a selftest
never hides a stopped audit. The TMDB status goes in only after a live TMDB answer. Useful alerts
are these:

- The tmdb status is `token_rejected` for an hour.
- TMDB stays unavailable for several hours, from the first failure to the last check. A single
  failure before a quiet night then never fires.
- The policy status is `failed`.
- `last_hook_run` is older than 2 days. The nightly audit writes the policy status every day, so a
  stale file means the audit schedule or the script stopped.

## Webhook

A Custom Script connection runs the command inside the app's container. In Docker, that container
then needs Python, ffmpeg and mkvtoolnix. So the image runs `arr-media-guard --serve`, an HTTP
listener in `serve.py`, and each app gets a Webhook connection to it. `hook()` reads a
`runner.Event` from the Custom Script variables, and the listener reads the same Event from the
body. Both queue its job through `queue_job()` and start a worker through `ensure_worker()`, so
the worker, the queue and the checks are one code path. A Grab goes from the Event to
`keep_grab()` in both, and a Test runs `app_check()` in both.

**The fields.** The job takes these values. The Webhook body has each field the Custom Script
variables give, and the listener still asks the API for the path.

| Job key | Custom Script variable | Webhook field | Source in the job |
| --- | --- | --- | --- |
| `owner` | `radarr_movie_id`, `sonarr_series_id` | `movie.id`, `series.id` | the body, checked against the file record |
| `file_id` | `radarr_moviefile_id`, `sonarr_episodefile_id` | `movieFile.id`, `episodeFile.id` | the body |
| `path` | `radarr_moviefile_path`, `sonarr_episodefile_path` | `movieFile.path`, `episodeFile.path` | the API, `moviefile/<id>` or `episodefile/<id>` |
| `release` | `radarr_moviefile_scenename`, `sonarr_episodefile_scenename` | `movieFile.sceneName`, `episodeFile.sceneName` | the API, the same record |
| `episode_ids` | `sonarr_episodefile_episodeids` | `episodes[].id` | the API, `episode?episodeFileId=<id>` |
| `download_id` | `radarr_download_id`, `sonarr_download_id` | `downloadId` | the body, text of at most 200 characters |
| `deleted` | `<app>_deletedpaths` | `deletedFiles[].path` | the body, each path in the item's folder from the API |
| `recycled` | `<app>_deletedrecyclebinpaths` | `deletedFiles[].recycleBinPath` | the body, each path in `recycleBin` of `config/mediamanagement` |

`eventType` is `Download` for an import and an upgrade, and `Test` for Test. With `KEEP_REPLACED`
on, a `Grab` body names the item in `movie.id` or `series.id`, the episodes in `episodes[].id`, and
the download in `downloadId`. The Custom Script variables of a Grab name the movie in
`radarr_movie_id`, and the series in `sonarr_series_id`. Sonarr names the episodes only by
`sonarr_release_seasonnumber` and `sonarr_release_episodenumbers`, so the hook asks the API for
their ids. Both apps name the download in `<app>_download_id`. The API gives the files. A live test used
Sonarr 4.0 and Radarr 6.4. Both send `deletedFiles[].recycleBinPath` on an upgrade. Sonarr's On
Import Complete trigger also sends `Download`, but with `episodeFiles` and no `episodeFile`. The
listener refuses it and says which triggers to use.

**Trust.** Each post needs HTTP basic auth, the only auth the Webhook connection sends. The apps
send it with the first request. The listener refuses a body over 1 MiB before it reads it. It takes
the file's path from the app's API by the file id, never from `movieFile.path` or `episodeFile.path`
in the body. The hook edits that file in place, and a body can name any path. The file record must
belong to the item the body names, and its path must be a plain absolute path. An old file must sit in the item's folder,
and its recycle bin copy in the app's recycle bin, because a restore renames the copy back over the
old path. A refusal answers 4xx or 5xx, and the decision log gets a line with source `webhook`. The
app shows the answer in its Test and in its log.

**No import is lost.** The listener queues a Download even when the app's API fails or this
container does not see the file, and writes one warning line for it. The app sends a notice once,
so a refusal would leave the import unchecked. When the API failed, the job holds the body, and
the worker runs the same checks and lookups when it takes the job. While the API still fails, the
job's row in the state store takes its next try and its new time in one change, so one row holds the job at every
moment. It joins the queue at that time, so it never holds
up the jobs behind it, and no worker runs for it before then. The first wait is a minute, and each
next one doubles, up to an hour. At the one-day age limit the job is dropped with one error line
that says the import was never checked. A 404 for the item means the app deleted it, so the job ends
at once with one error line. A file the container does not see is looked up again by
its id, as for a file the app moved, see `moved()`.

**Paths.** The image must see the media at the apps' paths, or a path map pairs the two. Each
program has its own map, `SONARR_PATH_MAP`, `RADARR_PATH_MAP` or `PLEX_PATH_MAP`. An empty one
takes `PATH_MAP`, so a setup from before the three maps works unchanged. A tester's setup showed
the need. Sonarr saw TV at `/mnt/TV`, Radarr saw films at `/movies`, and Plex saw them at
`/mnt/TV Shows` and `/mnt/Movies`. One map cannot pair those with the container's paths.

Each path that crosses to a program uses that program's map. An instance of `APP_INSTANCES` has a map of
its own, see "Instances". `arr()` and `arr_write()` map every path in an API answer to the local path, and
every path in a query or a body back to the app's path. The subtitle hunter and the conversion
reach the app through them too. The listener maps the posted file path and the old files of an
upgrade with the app's map. The Plex lookup, the folder scan and `--plex-flush` map the local path
to Plex's path. The lists `--plex-flush` reads hold local paths. Radarr's extra files come with
relative paths, which the script joins to a folder from the API, so they need no map. The hook's
Custom Script variables need none either, because a Custom Script runs where the app runs. The
subtitle hunter maps SABnzbd's download path with Radarr's map. A pair matches whole folder names,
so `/mnt/TV` never matches `/mnt/TV Shows`, and the longest pair wins.

`--selftest` and the listener start check each map against the app root folders. A root folder
the script does not see warns, with the app's map setting. A root folder that no Plex library
folder holds or sits in warns, with `PLEX_PATH_MAP`. The check starts from the root folders, so a
Plex library that holds none of them, such as music or another host's library, never warns. Both
are warnings only. A deploy may run `--selftest` while a mount or an app is down. The listener must
start before the apps answer, so it runs its start check in a thread. The thread runs the checks of
the Test event for each app with an API key, and prints one line per app to the container log. It
asks an app that does not answer again for 2 minutes. A failed check warns, and the listener keeps
running. A policy that did not load warns once, and each app is still checked. The path check runs
last in the thread. It leaves an app that does not answer and a root folder it does not see to the
app checks, so each warns once. Each API call of the Test checks waits at most 10 seconds, so a hung
app fails the Test fast.

After the apps, the start check and `--selftest` check each other service the setup uses, see
`runner.service_checks()`. A tester found that a wrong Plex URL or token stayed silent before. Each
check is one GET that changes nothing, and each prints one line. Plex lists its
libraries with `PLEX_TOKEN`. Discord answers a GET of the webhook URL with the webhook's details, so
the check proves the webhook and its token without a post. TMDB has a key check of its own. SABnzbd
reads one slot of its queue, which needs the full API key. The Newznab indexer answers `t=caps`. A
search would cost each indexer behind NZBHydra2 a hit of its daily limit, so no check sends one.
SABnzbd and the indexer count only for a Radarr whose API key reads, because the subtitle hunter
reads their addresses from Radarr. A service that does not answer is asked again for 2 minutes at
the start, as an app is. A failed service check warns in both, and never fails the selftest. A
token or an address that urllib cannot send fails at once, with the setting's name, and is never
asked again. The path check runs after the Plex check. So a Plex that starts beside the listener has
answered its check before the path check reads its library folders. When the Plex check warned, the
path check adds no line of its own. When the Plex check passed and the folder read still failed, the
path check says the folders went unchecked.

The image reads the apps only through their APIs, so it needs no app config folder, and the env file
or the environment holds each API key. No feature reads an app's database. A database shared between hosts is not safe,
and a container on another host keeps every feature.

**Settings in the environment.** Compose passes settings as environment variables, so each setting
may come from one, and the environment wins over the env file. `config.settings()` reads only the keys
of the settings, so another variable, as a systemd `STATE_DIRECTORY` or an app's `sonarr_*`, never
changes one. An empty variable counts as set, so a user can blank a key the file sets. The listener
never writes a `WEBHOOK_PASSWORD` or `WEBHOOK_USER` that came from the environment, because the
environment would win over the written value at the next start. The worker and the nightly jobs
inherit the listener's environment, so every program reads the same settings.

**Requests.** The listener serves 32 requests at once on a pool of threads. Each request has 10
seconds for its headers and body together, so a client that sends one byte at a time loses its
connection, and an idle connection gives its thread back. Up to 64 more connections wait for a
thread, and the listener closes one past that at once. Only an app's post with the right credentials
gets a line on stdout and, when refused, a line in the decision log. The listener counts every other
refusal, a wrong path, wrong credentials, a malformed or cut request, and prints one summary line a
minute at most. A live test with one byte every 5 seconds held the one-request listener of the first
build for 465 seconds, and a Sonarr import notice failed.

**The worker.** The listener starts a worker after each job, and again every 60 seconds while a job
waits and no worker holds `worker.lock`. So a job queued while a worker was stopping, or left by a
stopped container, still runs. The worker is a new program in its own session, `arr-media-guard
--serve --worker`, because a fork would copy the listener's threads and a lock one of them held. It
reads the env file and the policy at its start. A program run as `--serve` sets `config.SERVE`, so
the logfmt summary of each decision line also goes to stdout, the container log. The nightly audit
runs as `--serve --audit` for the same reason, and writes only its summary line there. In the image,
tini is PID 1 and passes SIGTERM, as `docker stop` sends it, to the listener. The listener sends it
on to the worker's process group and waits for it. A job that has not written goes back to the queue, and a flag edit or a re-grab ends
first, see "Crashes and stops".

**Daily jobs.** A host install runs the nightly audit from a systemd timer, and the audit removes
kept originals and grab links older than `KEEP_ORIGINALS_DAYS`. logrotate rotates the decision log.
In the image, the listener runs both once a day at `AUDIT_TIME`, the audit for each app whose API
key reads. When the container was down at that time, they run when it starts again that day.

**One-off containers.** Every other mode runs in a one-off container of the same image, or with
`docker exec` in the service. A one-off container mounts the same `/config` and the same volume `amg-state`, so
its file lock, its scan state and its decision log are the service's. flock works across containers on one host,
because both open the same file. A live test held the lock in the service for 20 seconds, and a
one-off apply waited for it. tini gives each mode the signals of a terminal. Python as PID 1 ignores a
SIGTERM it has no handler for. Without tini, `--plex-flush` ran on through a `docker stop` to its
end.

## Instances

One install serves several Sonarr and Radarr instances. `config.settings()` reads the instances
`radarr` and `sonarr` from the `RADARR_` and `SONARR_` keys, then each `name:program` entry of `APP_INSTANCES`.
The keys of an instance start with its name in upper case, with `-` as `_`, see `config.env_key()`.
`apps.ARR` holds one adapter per instance, keyed by its name. Its class is the program, `Radarr` or
`Sonarr`. `arr()` and `arr_write()` take the instance's URL, API key and path map. Every `app` value
in the code is an instance name, and `config.program()` gives its program where the logic differs.

**Names.** A name holds letters and digits, with a single `-` between two of them. So it is safe in
a URL path, a file name and a log line. Two names may not share their keys, and a name may not take
the keys of another setting, as `plex` would take `PLEX_URL`. A bad entry is left out with a
selftest error, so no event reaches an instance the user did not mean.

**Events.** In Docker each instance posts to `/<name>`. The listener compares the whole request path
with the instance names, and answers 404 to every other path without a log line. So text from a
request never reaches the API, a path or a log as an instance. On a host the Custom Script
variables start with the program, `sonarr_` or `radarr_`, for every instance. Sonarr and Radarr also
pass `<program>_instancename`, the Instance Name of Settings > General, at each event
(`CustomScript.cs` in both). `runner.hook_app()` maps a run to its instance. The one instance of a
program takes every run, so a host with one of each needs no setting. With more, the Instance Name
must match an instance name. Case does not count, and each run of other characters counts as `-`. A
run that matches no instance asks no app and queues nothing, and its Test fails with the names.
`runner.name_clash()` reads the Instance Name of an instance from `system/status`, the value the
Custom Script gets. A 4K Sonarr that kept the name `Sonarr` would run as `sonarr`, against the other
API with its own ids and paths. So the hook's Test checks the name of every instance of the program
and fails on a clash, because the misnamed app's Test reaches another instance. The selftest on a host
warns of a clash. The listener's Test, its start check and a selftest in the image skip the name,
because there the URL path picks the instance. The image says so with `ARR_MEDIA_GUARD_IMAGE=1`.
A connection saved before a rename never runs its Test again. So the hook keeps the run's Instance
Name in the job, and the worker reads the name of each instance of the program once per worker. It
refuses a job while a clash stands, with one error line that names the Instance Name to set. A job of
the listener, or of a program with one instance, skips this check.

**State.** Each record of one app in the state store keys on the instance name. That covers the `app` of each job,
the re-grab times, the pending conversions, the scan progress, the lists of `--plex-flush`, the decision rows and the
state of the subtitle hunter. So the re-grab cap counts per instance,
and an instance at its cap never stops another. A grab link of `KEEP_REPLACED` holds its instance.
An import claims only the links of its own instance. An import of another instance at the same path
makes them stale, because they no longer hold the file that the import replaced. The old files of an
upgrade come only from the queued jobs of the same instance, because file ids repeat between
instances.

**Output.** The `app` field of the decision log and the `arr` key of the logfmt line hold the
instance name. `status.json` holds no app. An alert posts as `<label> <INSTANCE>`, and the label is
the instance name with a capital first letter, such as `Sonarr`, `Radarr` or `Sonarr-4k`.

**Checks.** `--selftest`, the Test event and the start check of the listener run
`runner.app_check()` for each instance whose API key reads. The listener's nightly audit runs for
each such instance too.
