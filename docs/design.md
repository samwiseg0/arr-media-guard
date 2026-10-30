# Design

arr-media-guard runs on every Sonarr and Radarr import and upgrade, through a Custom Script
connection on a host or a Webhook connection in Docker. It makes the right audio track play first and sets the subtitle defaults to match. It checks that the
audio plays and the video decodes, and it re-grabs a file that is certainly broken. The same command
backfills the library and scans it for broken files. A second command hunts for releases with English subtitles.
Some releases of English films carry a foreign default audio track, and Plex plays the default.

This file says what each rule does and why it exists. The README covers the install, the env file
and the app connection. policy.md covers the policy file.

- A *job* is one file that waits in the background queue, for an import, a burn-in job, a whole-file check, a deep
  analysis, a recheck or a backfill. AMG queues it, and a *worker* runs it.
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
- **Audio role.** Main, commentary or description ("AD", "DVS"). Only a main track plays first.
  "Commentary", "cmt" or "Comm" in the title makes a track commentary. So does a *notes title*, which has
  "Production Notes", "Trivia", "Info Text", "Fact Track", "Pop-up" or "Easter Eggs", or is just "Notes".
  A title that negates commentary holds the dialogue (`NO_COMMENT_TITLE`). Such a title is "No Commentary",
  "Commentary-free", "Without Commentary", "w/o Commentary" or "Non-Commentary", also after a language or in brackets.
- **Subtitle role.** Commentary, dub transcript ("English (dub)", "Dubtitle"), lyrics, forced, SDH or full.
  A "forced" or signs title (signs, songs except on a lyrics track, foreign parts, alien, titles only) makes a
  track forced. So does a forced flag, or a density under `sparse_events` (1.5 events a minute). "SDH", "CC" or
  "full" in the title overrides the signs words.
- **Lyrics.** A subtitle whose title names songs and no other signs word, in the item's original language, holds
  the song lyrics. A viewer of the original audio hears the songs, so the track is never forced. A policy cannot
  list the lyrics role, so the track never becomes a default. A forced flag on it stays. A "Songs" track in another
  language translates the songs and stays forced, as in the English dub of a Japanese film. With no original
  language, AMG cannot tell lyrics from a translation, so the track stays forced. The Original language flag
  treats a lyrics track as a forced one.
- **Notes titles.** A notes title makes an audio or a subtitle track commentary, so the subtitle check leaves it
  alone. "SDH", "CC" or "full" in the title overrides the notes words. The word "notes" inside a longer title, as in
  "TL Notes", keeps the track's role.
- **Density.** The events a minute come from the NUMBER_OF_FRAMES tag when the app that wrote the
  file wrote it, as its `_STATISTICS_WRITING_APP` says. A tag that another app or an earlier write
  left is stale, and the walk's count of the track's blocks stands in (`checks.walk_frames()`), when
  the walk reads every Cluster and the job has time for a whole read. A track with no tag has no
  density. A density counts only when the file runs `density_min_minutes` (15) or more. A PGS count
  is halved, because PGS stores two frames per subtitle. A forced flag counts only under
  `forced_flag_events` (4). A forced flag on a denser track is a conflict (`dense_forced_flag`). So
  is a full or SDH track under 1.5 events a minute (`sparse_full_title`).

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

A Matroska track has a legacy ISO 639-2 tag (`eng`) and a newer BCP 47 tag (`en`, `en-US`).
`decide.track_tag()` reads the track's language from both. The BCP 47 tag sets the language when
its code is known and names another language than the legacy tag, as RFC 9559 says and Plex shows.
The legacy tag sets it in every other case. So `ger` beside `de` stays `ger`, and `chi` beside
`yue` stays `chi`. A legacy `eng` beside a BCP 47 tag with no known code reads as `und`, because
`eng` may be the Matroska default.

`decide.ISO639_1` maps a 2-letter BCP 47 language to its 639-2 code. It holds the 639-1 rows of
mkvmerge's language table. A 3-letter code is known when `decide.KNOWN_CODES` holds it. That set
holds every ISO 639-2 code mkvmerge lists, the 3-letter codes of `ALIAS` such as `yue`, and `und`,
`mul` and `zxx`. Another code, such as `zsm` (Standard Malay), is often a language inside a
macrolanguage. mkvmerge writes the macrolanguage (`may`) as the legacy tag of such a track. `unk`
names no language. A BCP 47 tag that does not parse has no code.

Twenty languages have a terminology (T) code beside the bibliographic (B) code that mkvmerge lists,
as `mya` beside `bur`. An MP4 file stores the T code. `decide.TWINS` holds the 20 pairs. It feeds
`ALIAS`, so `codes()` and `lang_key()` read a T code as its B code. It also feeds the names of
`language_table()` and `lid.code()`.

`decide.classify()` reads each track of a mkvmerge probe with this rule. So the default tracks, the
language edits, the Original language flag, the subtitle checks and the alerts use it. The track
lists that the remux checks compare use it too, see `checks.track_list()`. Some readers take the
tags as the file holds them. `decide.language_tags()` gives both tags to `retag()` for its votes
and its undo, and to `unapplied()` to verify an edit. `process.conversion()` matches the subtitles
of a converted file to the original's by their legacy tags. `remux.track_args()` writes the BCP 47
tag, else the legacy tag, as the `--language` of a remuxed track. The ffprobe branch of
`classify()` reads the one tag that ffprobe reports.

A file that holds only a BCP 47 tag reads as `eng` in mkvmerge's legacy field, because `eng` is the
Matroska default. So a music track that holds only `zxx` reads `eng/zxx`, and its language is
`zxx`. `mkvpropedit --set language=<tag>` writes both tags, in the same call as the flag edits.

A tag changes only when two signals agree and one of them is the heard or the read language. The
signals are the track's tag, a legacy tag that names another language than the BCP 47 tag, the heard
language of an audio track, the read language of a subtitle, and the title's language, see "Subtitle
text".
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
- `mul` and `zxx` stay, from either tag, and get no note. A `zxx` audio track is no main track of a
  language, so a decision never picks it to play first. An undecided or dropped plan gets no tag edit.

The undo restores both tags, with a second `language-ietf` edit or a `--delete language-ietf` where
needed. Plex shows the BCP 47 tag, else the legacy tag, and it shows `und` audio as English. Hearing
is slow, so a first backfill over many `und` tracks can take hours. The cache makes later runs fast.

## Original language flag

Matroska's `FlagOriginal` is set when a track is in the content's original language. `decide.retag()` plans it with
the language tags, as the mkvpropedit property `flag-original` (`decide.ORIGINAL_FLAG`). The content's original
language is TMDB's, see `decide.content_language()`. When the app lists another original language for the item, or
TMDB gives none or one with no 639-2 code, no flag changes.

- Only the roles of `decide.ORIGINAL_ROLES` change: main audio, and full, SDH, forced, dub and lyrics subtitles. A
  commentary or an audio description is no part of the content's own speech, so its flag stays.
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
- A track tagged by its BCP 47 tag alone has that tag's language. Its legacy tag reads `eng` beside it, so the two
  tags disagree, and its flag changes only on a heard or read language.
- A track in the original language gets 1. A 1 on a track in another language goes to 0. A right flag, and a missing
  flag that would be 0, stay. An edit is `[selector, new, old, ORIGINAL_FLAG]`, with old `None` for a missing flag,
  whose undo is `--delete flag-original`. `edit()` drops a no-op, and `unapplied()` verifies it, a missing flag
  reading as 0. Its rules are `original flag set` and `original flag cleared`, so the audit groups them as other
  flag edits.
- An undecided or dropped plan gets none, as for the tags. The deep analysis, a recheck and `--sub-time` on a file
  no app lists keep the flags, see `process.act()`. No recheck entry exists, so old files change only in a backfill.
  `cli.selected()` takes a file with one audio track, no subtitles and no tag to fix when its Original language flag
  may change, see `cli.TRIGGERS`. So a backfill sets its flag as an import does.
- An edit of this flag alone queues no Plex analyze, see `process.changed()`. Plex does not read the flag, and most
  imports get one. Any other edit queues the analyze as before.
- The decision line's `tracks` shows each track's flag as `orig`, 1, 0 or `null`, as the run found it.

## Commentary flag

Matroska's `FlagCommentary` marks a commentary track, so a player can name it. Many releases title such a track
"Commentary" and leave the flag out (owner 2026-10-26: set it on titled tracks). `decide.retag()` plans it with the
language tags, as the mkvpropedit property `flag-commentary` (`decide.COMMENTARY_FLAG`). It takes the path of the
Original language flag.

- An audio or subtitle track whose title reads as commentary (`COMMENT_TITLE`: "comment", "cmt" or "comm" as a word)
  and that lacks the flag gets it. A notes title (`NOTES_TITLE`, such as "Production Notes" or "Trivia") gets none,
  and a flag set already stays. AMG never clears the flag. A title that negates commentary ("No Commentary", see
  `decide.commentary_title()`) gets none.
- An edit is `[selector, 1, old, COMMENTARY_FLAG]`, with old `None` for a missing flag, whose undo is `--delete
  flag-commentary`. A header edit makes it, with no remux. A remux of the same run carries it (`remux.EDIT_OPTS`).
  `edit()` drops a no-op, and `unapplied()` verifies it. Its rule is `commentary flag set`, and a note names the
  track and its title. The change post says "Turned the commentary flag on for ...".
- An undecided or dropped plan gets none. The deep analysis, a recheck and a run on a file no app lists keep the
  flags, as for the Original language flag. A dry run plans the edit, and `cli.selected()` takes such a file by the
  `tags` trigger.

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
`alert_result` says `log only` for each one that did not post. A stop is the owner's own act. A finding that the stop
caused posts nothing, and `alert_result` says `not posted, the run was stopped`, see `logs.stop_caused()`. That is a
re-grab or a restore that the stop kept from its step, and a conversion whose remux it cut. A run that a stop ended
before its alert step keeps its re-grabs and the unreadable notice of its conversion in the findings, see
`process.stop_line()`. They post, because no later run finds them again. The change posts of `DISCORD_POSTS=all` go
out too. The decision line names the stop, and the next run of the file posts what it still finds. An alert whose text
fails still posts, with its title and its file, and the sentence `report.UNTOLD` in place of the error. Its line in
`alerts` keeps the error.

**Check and step.** `report.stage_fields()` gives each issue alert two inline fields under its text and above the item
field. Check names the check that found the problem, by the source of the record, see `report.CHECKS`. The sources are
`hook`, `burn_in`, `whole_file`, `deep_analysis`, `recheck`, `check`, `backfill` (the flag backfill), `backfill_job` (a
job a backfill queued) and `check_job` (a job of `--check --background`).
A conversion that a stopped run left has `worker` when the worker finds it as it starts, and `backfill` when a
`--convert` run finds it, see `convert.pending_recover()`. Stopped at names the step where the fix stopped, one of
`report.FIX_STEPS`, see `report.stopped_at()`. A finding with an action takes its step from `ACTION_STEPS`. A subtitle
finding takes it from the sentences that did not fix their subtitle, see
`LINE_STEPS`. Any other finding takes it from `FINDING_STEPS`. Each step there is a name, `None`, or a function of the
facts. `None` means the program tried no fix: a doubt, a check that only reports, a fix that a setting or a limit turned
off, and a dry run. A sidecar left in place names no step when its reason starts with a setting, see
`report.SETTING_LEFT`. A live-captioned track whose hearing stopped part way carries `hearing_stopped`, see
`subtitles.sub_findings()`. A conversion that finished, whose extras wait for the app to take the new file, stopped at
Replacing the file. Each subtitle counts at the furthest step its sentences name, see `report.line_keys()`. Subtitles at
different steps show one step a line, in the order of `FIX_STEPS`. A field with no value is left out, so an alert with
no fix tried shows Check alone. A held alert keeps the fields the import gave it. A change post has neither field, see
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
| `language` | No main audio track is English, the original language or a language TMDB lists. The text names the main audio only. An audio track in a wanted language with another role says why it does not count, as in "The English track is commentary." The last sentence names the source of the original language. That is TMDB when its original agrees, else the app (`process.file_alerts()`). |
| `runtime` | The trusted duration is far off the listed runtime, see "Metadata checks". |
| `duration` | The header duration is more than 40 percent off the stored end, or disagrees with the other duration sources. The stored end is where the last video or audio frame ends, see `checks.stored_end()`. A length from the size and a bitrate is an estimate, so no check takes it. A file with no stored end gets the bitrate ceiling: 50 Mbit/s up to 1080p and 150 above, by the film's height. |
| `episode` | The release names another episode than the one Sonarr imported it as. The alert leads with the imported episode and its title. |
| `content` | The evidence adds up to a re-grab. The title ends "re-grab is off" when `REGRAB` does not list `content`. |
| `audio`, `video` | See "Broken audio", "Corrupt video" and "Whole-file check". |
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
`would_regrab`, `check` (`--check` confirmed a fault that an import would re-grab), `unconfirmed`, `capped`, `no_grab`,
`failed`, `dry_run` and `no_policy`. The title of a re-grab says
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
- `packets` reads the time of every frame and decodes nothing. It finds where the audio, the video
  and each subtitle end, and the longest hole in the audio. Matroska, MP4 and TS files give the times
  they store, see "Time reads". AVI and a format with no reader give ffprobe's packets. The Matroska
  walk reads on past damage, as ffprobe does, so audio that comes back after the damage still counts.
  A TS read that stops at damage gives no verdict. An MP4 sample whose bytes run past the end of the
  file counts for nothing, because ffmpeg's read stops there.
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

Each window logs the steps between its frames in `steps`. The check holds the steps of the three windows against
their median, as the header repair does. See "A variable frame rate" under "Header repair". Here a step of a whole
number of usual frames, within the same bound, counts as on the rate. A web stream can leave a frame out at a join,
which makes a step of two frames. The three windows of a 60 fps web episode held 891 steps. 6 such steps and 3
steps of 22 ms were 1.01 percent of them. The import alerted on a constant rate. Only the 3 steps of 22 ms count now,
0.34 percent. So an encode that dropped repeated frames all through the track posts no alert either. Nor does a part
at half the rate, such as 30 fps in a 60 fps track. The decision line keeps
the share of steps off the usual frame length and the verdict in `video.frame_rate`. An import whose video check finds
a variable rate posts one amber "Variable frame rate" alert, the finding `frame_rate`. A fault or a doubt of the
check posts in its place. The alert changes nothing. A job a backfill queued, `--check` and a `--check-video` scan only
log the rate. So a library pass posts no alert per file. In 229 library files read in full, 5 hold a variable rate.
The three windows named each of them and no other file. With 1116 more files read at the same three places, about
190 of 34,000 library files hold one (0.55 percent). They are mixed-rate HandBrake encodes, DVD MPEG-2 with repeated
fields, and encodes that dropped repeated frames.

Clean encoders pad easy frames with short zero runs, so only a run of 256 KiB counts. The zero probe
skips the Attachments and anything past the Segment end, where fonts and old copies can hold zeros.
The doubt of one hit names a play time with the words of the whole-file check, see "Play times" under
"Whole-file check". The decision line keeps the hits as shares of the file size in `video.zeros.hits`
and their times in `video.zeros.when`. Only a file with a hit reads the Cues for them.

In a Matroska, MP4 or MOV file with a PCM audio track the zero probe's hits only log, with no alert and no re-grab.
Digital silence in PCM audio is zero bytes, so the probe defers to the whole-file check, whose read pass confirms each
run. The windows run whatever the probe found, so two bad windows are still certain. The hits still block a header
repair. The rule sits in `checks.check_video()`, so the import, the second check before a re-grab, `--check`, a
backfill and a scan all follow it. In Matroska, PCM comes from the mkvmerge probe the caller has, by codec_id
`A_PCM/...`. In MP4 and MOV mkvmerge names some PCM by its fourcc (`ipcm`, `fpcm`, `fl32`, `in32`), so ffprobe decides
there, as it does when the caller has no probe. Another container keeps the probe's verdict, see "Whole-file check".
Three windows rarely meet a small hole, so they are for damage spread through the file. An encrypted
track has no decoder, and neither does a codec ffmpeg does not know. So such windows are certain
only with evidence of encryption, such as an `encv` FourCC, a ContentEncryption element or an MP4
`tenc` box.

A *cover picture* is the poster of the film. It can come as a video track of a still-image codec (MJPEG, PNG, BMP or
GIF), and that track may come before the film. The checks take the film: the first video track that is no still image
and no attached picture, see `decide.main_video()`. A file of still images alone takes its first one. The windows,
the burn-in samples, the DURATION tag of the video end and the frame count of a header repair all read it. An ffmpeg
seek goes by the index of the first video track, so with a still image first the film would decode from a frame that
is no keyframe. Such a file's windows go through `avwindow.py` in the venv of language detection instead. PyAV
seeks by the film's own track, and the script writes its lines in the shape of ffmpeg's, so the same rules judge
them. The script discards every other track, as ffmpeg's `-map` does, so damage in an audio track is no part of the
window. Each libav line goes out when libav logs it, so a window that the caps kill keeps its lines. A decoder that
does not open makes the window one that could not run, as in ffmpeg. Each such window logs `via: "pyav"`. Every
other file keeps the ffmpeg windows. A host with no language detection install has no PyAV. There the windows do
not run, and `video.windows.skipped` says why. A header repair of such a file runs no window either, and
`windows_skipped` says why. A window whose Python does not start could not run, and its `error` says why. The zero
probe still runs.

A Segment duration can run far past the video. So the windows sit on the video end read from the
last Clusters, else mkvmerge's DURATION tag, else ffprobe. A file that is not Matroska gets a
doubt when its format duration runs over 60 seconds past its video end (`AV_APART`). The doubt names
the longest stream by kind, number and codec, as "audio track 2 (ac3)", when that stream ends so far
past the video. A window stops
at 60 seconds or 512 MiB read, because a seek with no index reads from the file start. A time-cap
stop is a doubt, and a read-cap stop with no error is only logged. A stop with errors is a doubt that
names the playback errors the window met, or the error lines of a window with no frame. In the hook each stage needs time
left and keeps `VIDEO_RESERVE` for the edit. An exception gives `video_check_error`, and the edit
goes ahead.

A certain fault re-grabs like broken audio. The second check moves its zero reads by half a step and
its windows to 30, 70 and 95 percent, and it must find the same fault class. So a local fault may
stay an amber "Broken video, not confirmed". It counts against the same `REGRAB_CAP` as broken audio.
Decode each certain file and each doubt of a `--check-video` scan in
full before you act on it.

## Whole-file check

The zero probe and the windows read about 16 MiB of a file. A torrent piece that never downloaded leaves a gap of zero
bytes, and a few gaps of a megabyte or two in a large file pass both. So each import queues a *whole-file check* job in
the background queue. It reads the whole file once, and it never runs on the import path. An import whose own video
check was certain queues none, and neither does one whose re-grab deleted the file. In a file with PCM audio the
import's zero probe defers to this check, see "Corrupt video".

| Pass | What it reads | Certain | Doubt |
| --- | --- | --- | --- |
| zero scan | every byte of the span the zero probe reads. A zero run counts from 256 KiB. | runs at 2 or more places | a run at 1 place |
| read pass | `ffmpeg -v error -i FILE -ignore_unknown -map 0 -c copy -f null -`: every packet of every stream but a TrueHD core with no sample rate (below). It decodes only the frames of ffmpeg's probe of the streams. | never | a line it prints, but audio lines under a bound with clean samples (below) |

`checks.whole_check()` starts the read pass in an ffmpeg process of its own. Then `checks.zero_scan()` reads the same
file in the job's process. The slower reader takes the file from the page cache, so the NAS sends it about once. The
scan tests each 64 KiB block of a grid from the span start. A run of 256 KiB holds a whole block, and `zero_run()`
measures the run around it. A read that comes back short is read on, so the grid never moves.

**Silent PCM.** Digital silence in PCM audio is zero bytes too. mkvmerge puts 5.1 and 7.1 audio of 24 bits in blocks
of over 256 KiB, so each second of silence leaves about three zero runs of valid payload. In Matroska, a real gap that
covers a block edge breaks the container, and the read pass names a byte inside it. So in a Matroska, MP4 or MOV file
with a PCM audio track, by ffprobe's format and codec names, a run counts only when the read pass names a byte within
one grid block of it. Each such byte confirms only its nearest run, because a silent run can sit beside a real gap.
The other runs are logged only. They never alert and never re-grab. In a file with PCM audio the read pass runs to its
end, because it confirms the runs. In a file with no PCM audio every run counts, and a certain scan stops the read
pass.

The rule has known misses. In a PCM Matroska file, a gap smaller than the block it falls in leaves the container
whole, so it is logged only. In MP4 and MOV the read pass names no byte inside a gap, so with PCM audio every run
there is logged only. In AVI, MPEG-TS and VOB the read pass named no byte inside a gap either, and silent PCM left no
zero run. So in those containers every run counts, whatever the audio.

A healthy file prints nothing in the read pass, except at zero padding (below). Every line counts, except the lines the
windows skip too: ffmpeg's repeat counter and the null muxer's complaint of a repeated timestamp. That complaint names
no byte of the file, and a healthy episode printed it. A remux printed it once a packet, so the stderr goes through a pipe and only the other lines stay.
A line that names a byte within 1 percent of the file of a run that counts belongs to that run, so it adds no doubt.
`-ignore_unknown` leaves out a stream of a type ffmpeg does not know, as in some MPEG-TS files. `-map -0:N` leaves
out the AC-3 core of a Blu-ray TrueHD PID when it has no sample rate as ffmpeg opens the file, see "Blu-ray TrueHD"
(`checks.truehd_cores()`). A PID with no core PES gives such a core, and so does one whose core PES start after
ffmpeg's probe. The null muxer cannot write a header with it. Up to 2.18.5 the pass then read nothing and printed one
error. So an unconverted TrueHD M2TS alerted "video may be broken". The TrueHD stream of the PID stays in the read, so
the demuxer still reads every PES of the PID. Any other stream with no sample rate stays in, and its header error is
a doubt. A file that ffmpeg cannot open, or whose streams it cannot set up, gives no doubt. Neither does a read pass
that runs over `WHOLE_MIN` seconds or the time the file needs at `WHOLE_RATE`, whichever is longer.

**The stream of a line.** ffmpeg names the logger of each line, as `[eac3 @ 0x57ad]`. `decide.copy_kind()` names its
stream from that name. A decoder or parser of `AUDIO_CODECS` or `VIDEO_CODECS` is audio or video. So is ffmpeg's logger
of an audio or video input stream, as `[aist#0:1/eac3 @`. A demuxer, ffmpeg's logger of an input file
(`[in#0/matroska,webm @`) and a line that names a byte are the container. The read pass decodes only the frames of
ffmpeg's probe of the streams. In made-up media on ffmpeg 7.1 that was the first packet of an E-AC-3, AC-3 or AAC track
and the first 7 H.264 frames. A DTS decoder decoded on while its frames failed, up to about 5 s. The h264 parser
printed lines from any place of the file. So did the TrueHD and MLP parser, for each bad frame. The other audio
parsers printed none, in MKV, MP4 and MPEG-TS. In 900 random garbles of the first frame of E-AC-3, AC-3, AAC and DTS,
one bad frame printed at most 2 lines. mpv played past it with no lost samples. An E-AC-3, AC-3 or AAC track with
every frame bad printed the same 1 or 2 lines. mpv played it as noise or silence.

So the lines of one audio decoder, with ffmpeg's repeat counts, post "Audio may be broken" over `AUDIO_BAD` (10).
The doubt says "the start of the audio failed to decode, with 2 errors". A TrueHD or MLP line can come from any place,
so their doubt names no place, as "a full read of the file found 1800 audio errors". The read pass keeps its first
1000 lines, and the count adds the audio lines past them. A TrueHD track bad in its middle printed 1800 lines.

Under the bound, `checks.audio_samples()` takes one sample of each audio track. It leaves out a TrueHD core that the
read pass leaves out, and the track numbers count the other audio streams, as a player lists them. A sample decodes
20 s from 10 percent of the duration, as the import's first audio sample does. A file shorter than that is sampled
from there to its end. The seek lands on the keyframe before the start. In a file shorter than about 10 keyframe
intervals, as 20 s with a keyframe every 2 s, that is the first keyframe. The sample then decodes the bad first frames
again. 6 bad first DTS frames posted that way in a 15 s file with a keyframe every 5 s. A sample opens the file, so
its probe prints the lines of the read pass again. A sample with more than `AUDIO_BAD` errors past those lines posts
"Audio may be broken" and names its track, as "A 20 s sample of audio track 2 found 1198 decode errors." A sample with
up to `AUDIO_BAD` errors past those lines logs only. ffmpeg stops a sample on its error rate when every frame of the
track fails, and its errors still count. Another sample that does not run posts the doubt of the read pass and says
so. Else the doubt logs only. The import's own samples miss such a track. They decode only the track that plays. A
playing E-AC-3 or AC-3 track with every frame bad decoded to noise at full length there, so `audio_verdict()` found
nothing. The sample runs only for a file whose read pass printed an audio line. That was 2 of 319 live checks in 11
days.

Video and container lines keep their doubt and its alert, "Video may be broken". A file with both gets one alert, "Video
may be broken", and its text names the audio doubt when that one posts. The decision line keeps the lines of each kind
in `whole.ffmpeg.kinds`, each audio decoder and its lines in `whole.ffmpeg.audio`, and the audio doubt and each sample
in `whole.audio`.

**Zero padding.** Some releases end with a few hundred zero bytes past the Segment end. The hook's flag edit up to
2.18.3 could grow the Tracks, and mkvpropedit then wrote them at the end of the file, after the zeros. The Segment then
holds the zeros, and the read pass names the first one. `checks.zero_pad()` drops that byte when it starts the one zero
run after the Cues. Only whole elements that are not Clusters may lie from the Cues to the Segment end, and at least one
must follow the run. No player reads those zeros. The decision line keeps the byte in `whole.ffmpeg.padding`. An unknown
Segment size, a Segment end past the file end, no SeekHead that lists the Cues, Cues before a Cluster and a run of 256
KiB or more keep the doubt. When mkvpropedit adds chapters, it can move the index to a second SeekHead at the end of the
file, which the front one lists. `checks.seeks()` reads that one too, as the header check does. It follows a chain of
such SeekHeads and reads each one once, 8 at most, so SeekHeads that list each other end the read. Since 2.18.4 a flag
edit of a file with bytes past its Segment end runs as a remux, so it leaves no such zeros, see "Write stage". Zeros
that an older edit left are a header issue, and the header repair drops them, see "Header repair".

**The tail fault.** Bytes past the Segment end make ffmpeg read the last element again, see "RFC 9559 check". In a file
with such bytes, read pass errors that all sit past the last Cluster are the tail fault. When the RFC 9559 check finds
`tail_errors`, the "Truncating packet" lines are the tail fault too (`decide.TAIL_LINE`). Every other line that names no
place stays a doubt. Damage in the payload of the last Cluster prints such lines, as "Invalid NAL unit size".
mkvmerge copies that damage with no warning, so the header repair cannot end it. The tail fault adds no doubt, and the
note of the decision line says the header repair removes it. The decision line keeps the count of those bytes in
`whole.ffmpeg.tail` and the check's answer in `whole.ffmpeg.tail_errors`. `whole.ffmpeg.truncated` counts the
"Truncating packet" lines. The same errors in a file with no such bytes stay a doubt.

**Play times.** The doubts of the whole-file check and of the import's zero probe name play times, from the Cues, see
`checks.play_times()`. So a gap reads the same whichever check found it. The read pass names the time of each byte, one
blank gap names the time of its first byte, and the probe names the time of the zero page it read. A byte gets the
Timestamp of the last Cluster that the Cues name at or before it, from a 32-byte read of that Cluster's start. So the
time is never more than a few frames later than the damage. Reordered frames can time a block a little before its
Cluster's Timestamp. A Cluster that the Cues do not name makes the time earlier. The first cue time of a Cluster can
be seconds late, where the Cluster starts before its first cued keyframe. The Clusters after the last named one are
walked, up to 64. A byte before the first named Cluster is "before the video starts", and a byte past the last Cluster
is "after the video and audio end". When the walk reaches neither the next element after
the Clusters nor the Segment end, a byte past the Clusters it read gets no time. Neither does a byte of a Cluster
whose Timestamp does not read. A byte with no time names its share of the file size. So does every byte of a file
with no SeekHead that lists the Cues, and of a file whose Cues do not parse to their end, as when the last piece of
a download is lost. The decision line keeps the bytes in `whole.ffmpeg.pos` and `whole.zeros.runs`, and the
times in `whole.ffmpeg.when` and `whole.zeros.when`. The whole-file check's alert also says that AMG kept the file and
did not re-grab it.

A certain verdict re-grabs the job's file alone, with `REGRAB`, `REGRAB_CAP` and the restore of an import's re-grab.
Each other file of the download has a job of its own. Under the exclusive file lock, the second check measures the runs
that count again with `zero_run()`. It reads at most 512 KiB a run, and never the whole file again. A doubt only alerts.
When the import's own probe found the one gap and its alert named it, the job logs it only. The alert has a marker of
its own, so an earlier video alert of the file never holds it back. A file that changed or went during the scan gets no
verdict, so a doubt of the old file never posts against a new one.

A conversion, a header repair or a subtitle remux of the import writes the file again, and a remux reads past the
damage and leaves the zeros out. So the job reads the original that the first remux kept, while it is kept. In the
decision line, `whole.file` names the file the job read and `whole.remux` names the remux. With no kept original the
job reads the remux, and `whole.original_gone` names a kept original that went.

The job reads with no file lock. It yields to an import that waits, and starts both passes again on its next run. Its
source is `whole_file`, and its outcome is `corrupt_video`, `video_doubt`, `audio_doubt`, `video_checked` or `file_changed`. `--check`
runs it right after its own checks and never re-grabs. A job that a backfill queued queues one too, which alerts only.

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

`HOOK_WORKERS` (2) sets how many import jobs run at a time. `BACKGROUND_WORKERS` (1) sets how many jobs of the
background queue run at a time, see "Deep analysis". Set the two to about the cores the host can spare.
When either is more than 1, the worker claims each job by one change in the state store, forks one process
per job, and sends every Plex analyze itself, so the idle-section gate holds. The worker runs at most the larger of the
two at a time, `config.slots()`. An import that finds every place held by jobs a backfill queued starts in one more
process, see "Backfill". A season pack of 200 episodes runs at most `HOOK_WORKERS` processes at a time. A job process that crashes writes one decision line with
outcome `error`, the exception and a short trace, because its stderr is often `/dev/null`. A job whose processes crash
`CRASH_TRIES` times in one worker run is not claimed again in that run, also when the store lost its crash count.

**No store connection crosses a fork.** SQLite keeps the locks of a process in its memory. A forked child that opens
the store while a copy of its parent's connection lives in it takes no real lock. Another process can then checkpoint
and delete the WAL that the child writes, and the store breaks ([How To Corrupt An SQLite Database
File](https://sqlite.org/howtocorrupt.html), section 2.6). So the hook and the worker fork through `store.fork()`. It
closes the connection of the process first, and it refuses the fork while any descriptor still holds a store file.
`db()` raises when it finds a connection of another process. The hook and the worker fork with one thread. The
listener never forks, and the thread pools of a backfill or a scan share one process, which SQLite supports.

**Time limit.** A job has 300 seconds from the file lock to its write. mkvpropedit has no limit,
because a kill mid-write can break the header. A remux, its proof and its swap have none either. The
checks after a conversion or after a header repair that runs before them get a new 300 seconds. The limit is a
deadline, `content.DEADLINE`. Each subprocess, HTTP call and lock wait under it ends at it, and a long read checks it
between two blocks. It raises `content.OutOfTime`.

The time limit ends the job. `OutOfTime` is a BaseException, so no `except Exception` takes it, and every other step
of the job stops there. Six handlers name it and turn it into an outcome. `runner.run_job()` records the outcome
`error`, and the line keeps what the run wrote, so Plex still analyzes a changed file. `cli.backfill_file()` records an
error line for a backfill or a --check file. `process.content_checks()` ends the metadata checks and its re-grab lock
wait with `meta_error`, because the edit is done. `process.quick_check()` leaves the file unsure. The old file check of
`regrab.restore_plans()` keeps that old file in the recycle bin. `checks.video_check()` gives `video_check_error`,
because a failed video check never costs the edit, and the run then gets a new limit. The stop line and the alerts at
the end of a run stop the limit, so their posts have none. tests/test_time_limit_handlers.py pins each place in the
source that can take `OutOfTime`, so a new one fails the test until someone classes it. The
metadata checks after the edit get a new 300 seconds. The quick burned-in subtitle check gets the time they leave, see
"Burned-in subtitles". Every error is logged, and the import never sees one.

**The file lock.** Every job, backfill and scan takes `STATE_DIR/lock`. An import job waits an hour at most.
Reads take it shared. An edit, a re-grab and a conversion's swap take it exclusive. Every taker
first passes `lock.gate`, so a waiting edit stops new readers and waits only for the reads in
flight. Linux flock gives a waiting exclusive lock no preference, so without the gate a scan could
keep an edit out. A taker lets the lock go before it passes the gate, so no taker waits at the gate with the lock held.
flock cannot upgrade a lock, so a job that takes it exclusive checks the file's
inode, size, mtime and the re-grab unit of its download again, and runs again when one changed. Hearing holds no
lock, because it may wait for a turn at the model. A conversion in a job process builds the new file under the shared lock,
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
import left. The worker starts one only when no import job waits, `BACKGROUND_WORKERS` at a time per host, and never
drops one by age. It never runs two jobs of one file at once, see `runner.next_jobs()`, because a burn-in mute or a
remux of the file would make a whole-file check of it drop with no verdict. A
job drops itself with one decision line when its file is gone or has another inode, as after an upgrade, whose import
queues a job of its own. When the file is gone at its start, the job first asks the app by the file id where it is now,
see `runner.app_file()`. After a rename or a move, which queues no import, it goes on at the new path and logs a
`file_moved` line. An edit in place keeps the inode, and the job goes on. It checks this when it starts, each time
it takes the file lock, after its remux and after an error, because the app holds no lock. A change of its own never
counts. A conversion of its own before a yield gives the job the new file's inode, size and mtime. The queue drains with
no new import, because the worker runs until both queues are empty. A deep analysis has no time limit. Its whole-file
hearing hears two windows at a time, and between two of them it yields when an import's hearing waits at `lid.turn.gate` or an import
job waits for its place, see `runner.import_waits()`. The worker that runs each job itself yields to every waiting
import. With job processes, a job yields only while every place is taken and the imports have a place of theirs left.
One change of the state store marks the job yielding, see `lid.YIELD`, so of the jobs that ask at once one alone
yields, and the other jobs go on. That change itself reads whether an import still waits, so no job yields to an
import that took a place meanwhile. A job marked yielding stops at its next step. It also stops for a waiting import job after the whole-file read, before each read and each fit
of a track, after each 10 minutes of audio of the speech read, see "Incorrect subtitle identification", and before the
remux. It then goes back to its queue. Its next run finds the words heard so far in the cache, and the whole-file read
in the state store. With `HOOK_WORKERS=1` and `BACKGROUND_WORKERS=1` an import job so waits at most for one step: the whole-file read of one film,
the read or fit of one track, one pair of windows, 10 minutes of audio of the speech read, or one remux. With more
workers it runs in a job process of its own. A deep analysis decides with the inputs its import stored in the job: the
original language, the release name, the kids flag and the rest, so it asks no app for them. It never hears the language
again. The job names the import's job in `from`, and its decision line and syslog line carry it. It also holds the
subtitle alerts its import held for it, see "Alerts".
It keeps every flag the import set, after a remux of its own too. Only a subtitle verdict changes a flag: a track whose
words do not match the audio loses its default and forced flags.

**Burn-in job.** When the quick burned-in subtitle check flags a file, or cannot tell, the import queues a burn-in job
for it, see "Burned-in subtitles". It works like a deep analysis job. Its name comes from the path, and it records the
inode, size and mtime of the file. It follows a move, drops itself when the file is gone or replaced, and yields to an
import. Its name starts like a deep analysis job's name, so it waits behind the imports and shares the
`BACKGROUND_WORKERS` places with the deep analyses and rechecks. Burn-in jobs run before deep analyses, so the burn-in job of an import ends before its deep
analysis starts. A repair inside the flag edit of a burn-in job replaces the file, see "Write stage". The queued deep
analysis and whole-file check of the file then take its new inode, size and mtime, so they do not drop.

**Whole-file check job.** Every import queues one, see "Whole-file check". It works like a burn-in job, and
`runner.follow_up()` runs both. In each group of the queue it runs before the deep analyses, because a subtitle remux of
a deep analysis replaces the file and drops the job.

**Crashes and stops.** A job process that dies goes back to the queue, and the third crash drops the job. SIGTERM
starts no new job, kills each child ffmpeg or mkvmerge, and puts the job back. A job inside mkvpropedit or a re-grab's
deletes blocks SIGTERM until they end. A stop during a remux removes its temp file, and the original stays. That
covers a header repair, a subtitle remux, a conversion and the layout repair right after a flag edit, which keeps the
edit. The remux hands the stop to the process's own handler, see `remux.Swap.stop()`. A backfill with one worker or
`--sub-time --apply` then ends after the file, see `cli.stops()`. A job process that has written ends the step in
flight and writes its line. One that has not written stops at once, and its job goes back to the queue. Once a handler of the
process's own took a stop, no write starts, see `runner.refuse()`. `docker stop` gives the container 60 seconds before
SIGKILL, and a remux or a check can take minutes. A remux and a conversion refuse to start in `remux.Swap.start()`,
and mkvpropedit and a re-grab in `runner.no_stop()`. No further step of `process.process()` runs either, and a re-grab
runs no second check, see `runner.stop_asked()`. The decision line then comes at once, and `not_run` lists the steps
left out, see `process.stop_line()`. A run that stopped before its decision gets the outcome `stopped`, "not checked:
the run was stopped", with the reason codes of what it wrote. No other code says that the decision never ran. The Plex
analyze of a changed file still runs. A stopped or refused header repair gives `header_not_repaired`. A stopped
subtitle remux gives `subtitle_remux_failed`, and a stopped conversion `repack_failed`. A refused flag edit gives
`edit_failed` with "not edited: the run was stopped", and a refused re-grab the action `failed`. A stop posts no
alert about itself, see "Alerts". A job process then keeps its job claimed, see `runner.drop_job()`, and exits as a
stopped one. Its job goes back to the queue, and its next run reads the file as the stopped run left it and makes the
rest. That next run queues the jobs that follow an import, see `runner.queue_after()`. It settles the extras that a
converted import left hidden, see `convert.left_converted()`, and sends the app rescan of a write of the stopped run.
A backfill sends that rescan at its end. A deep analysis that a stop cut keeps its held alerts for its next run.
A job process, a remux and a backfill apply send SIGTERM and Ctrl+C to one handler, `ff.on_stop()`. A dry backfill
sends Ctrl+C to it. It only records the stop, so it never leaves a lock held. CPython runs a handler between any two
bytecodes of the main thread, also inside a `with` block of the standard library, and a raise there skips the release.
After a write, and in a backfill apply, the stop only asks. Before a write, during a remux and in a dry backfill, the
stop cuts the step. The handler then kills each child of the
process with SIGKILL. The job raises the stop at its next check, see `ff.check_stop()`. A check comes before each
child starts, after each child of `ff.run()` and `ff.popen()`, at each check of a time limit and at each try of a file
lock. The Matroska walk and its frame pass, the packet read's frame loop, the TS read, the proof's wait for its reads,
and the waits for another job's turn, temp file or swap check too. A sleep runs 10 seconds at most before the next check. An HTTP call in flight runs to its timeout, 60 seconds for
an app. When mkvmerge fails after such a stop, the failure counts as the stop, because the stop killed mkvmerge, see
`remux.Swap.stop()`. A run with no handler of its own, as `--check` and the worker with `HOOK_WORKERS=1` and
`BACKGROUND_WORKERS=1`, has Python's Ctrl+C handler and SIGTERM at its default. It keeps a stop during the layout repair
in `STOP["kept"]`, and only its next write raises it, see `runner.raise_term()`. The checks before that write let the
run go on, so the decision line of the edit gets its store row. A stop during any other remux ends such a run at once. Pending Plex analyzes go into the state store for the next worker. With `KillMode=process` on the app's unit, a stop of the app
never signals the worker. A decision log line is one `O_APPEND` write, so lines never
interleave. When the app renamed a file before its job ran, the worker asks the app for the new path
by the file's id. A 404 or another item drops the job as `file_gone`. When the file is at its path, each run of the
job asks the app by the file's id too. The run asks under the file lock before the checks, and a re-run after a re-plan
asks again. A second import of one release can delete the job's file record and write its file to the same path. A 404
or another item then drops the job as `file_gone` with no alert, and the note says the app replaced the file. The
newer import's own job checks the new file. Another API error ends the job as an error. The `ManualImport` of a
conversion gives the file a new id, and so does an undo that imports the original again. The queue keeps the new id
when the app gives one, so a re-run asks the app for it. With `HOOK_WORKERS=1` and `BACKGROUND_WORKERS=1` a job holds
the lock exclusive and never re-plans, so a replace during the checks still reads the copy. The worker then also runs a deep
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
`lid.turn.<n>`, `lid.turn.gate`, `worker.lock`, `subhunt.lock`), and the lists for a person: `convert-<app>.txt` and
the scan's `<kind>-scan-<app>.txt`, or `<kind>-scan-<app>-ids.txt` for a scan with `--ids`.

In Docker, `STATE_DIR` is `/config/state`, a named volume of its own. SQLite in WAL mode needs a local disk, and
`/config` may sit on a network share.

## Memory

The worker forks from the app, so it and its children (ffmpeg, mkvmerge, language detection) run in
the app's cgroup. Under the systemd default `OOMPolicy=stop`, an OOM kill of any child stops Sonarr
or Radarr, and `Restart=on-failure` starts it again mid-import. Set `OOMPolicy=continue` on both
units, then run `systemctl daemon-reload`. The app needs no restart, because systemd reads
`OOMPolicy` when the OOM event arrives. Each turn at the language model takes about 0.8 GB, so `BACKGROUND_WORKERS=4`
can take about 3 GB while the background queue hears.

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
its download deleted logs `deleted_with_download`, with the fault kind of that re-grab in `fault`. An error line of a run
of `process.STEPS` has an `id`, the exception in `result` and its first frames in `trace`. The frames reach the step that
raised, and its line.

The outcome, reason and Plex codes are stable, so queries and alerts can match on them. Each step
that makes a result gives its outcome code with it, and a result with no code logs `other`. The
reason codes come from the `say()` calls in
`decide.py` and from the header and restore steps. An `editing` line with the undo goes
out before each edit, so a crashed edit still has a record. A repack, a header repair and a subtitle
remux each write a line before anything else runs, with the code of the step in `outcome`. A subtitle remux
that changes no flag gives the decision line the outcome `subtitles_remuxed`, and its result names each change, as
`subtitles remuxed: s2 retimed, s1 lines moved`. When only sidecars changed, the outcome is `sidecars_changed`, as
`sidecars changed: Film.en.srt retimed`. A header repair that changes no flag keeps the outcome `no_change`, and its
result names the repair, as `header repaired`. A dry run that plans a header repair, a subtitle remux or a sidecar fix
and no flag edit keeps `no_change` too, and its result is `dry run`, see `process.quiet_result()`. So the counts stay.
Only a decision line has `schema`. A missing or broken policy file never
stops the hook. It logs `no_policy` and alerts once. A backfill or an audit refuses to start. Rotate
the log weekly with compression. The code never reads the log back, see "State".

Each decision also goes to syslog as one logfmt line with the tag `NAME`. It holds no path, no track
list and no secret. The app key is `arr`, because a log store such as Loki often puts the syslog tag
in its `app` label. A deep analysis line adds `from`, the job of the import that queued it.

```
arr=radarr source=hook outcome=edited class="English original: audio switched" edits=2 reasons=kids_dub alerts="" tmdb=found label="Film A" id=65ab6056ca69
```

## Backfill

A backfill runs the hook's checks over the library. It is dry unless `--apply` is given. With no
instance it runs every instance in turn, `radarr`, `sonarr`, then those of `APP_INSTANCES`, with one
`== <instance>` line before each. It skips an instance the user did not set up. An instance that
fails, as one that does not answer, ends with one line, and the run goes on to the next one and
exits 1 at the end. `--ids`, `--paths` and `--force-convert` need an instance. SIGTERM or Ctrl+C
stops the whole run.

**The queue.** With no pass flag (`--convert`, `--sub-check`, `--check-audio`, `--check-video`) and
no plan option (`--plan-out`, `--plan-from`), an apply puts one job per file the app lists in the
background queue, see `cli.backfill_queue()`. `runner.queue_listed()` writes it as it writes a
recheck: the app's decision inputs, the ids and the `file_key()` of the file, under `deep_name()`,
with `"backfill": true`. A job of the path that waits or runs stays. The pass takes the files in file
id order. It keeps its place in the state a scan keeps, as `queue-<instance>`, see `cli.pass_left()`
and `cli.advance()`. A file that keeps a job of its path counts as taken, unless that job is a
recheck. A file missing on disk, or whose path holds a recheck, stays in the pass for a later run.
`--limit N` queues the next N files, a run after the pass queues nothing, and `--restart` starts a
new pass. An `--ids` or `--paths` run keeps a state of its own, which starts again when they change.
A dry run writes no state and no `status.json`, queues nothing and probes no file. The worker takes
these jobs after the burn-in jobs, the whole-file checks and deep analyses of imports, and the rechecks, see
`deep_analysis_queued()`.

`runner.deep_analysis()` runs a job with `backfill` as `--check --apply` runs a file. It calls
`cli.backfill_file()` in import mode with the job of `cli.check_job()`, under an import's time limit
and with no download. Its source is `backfill_job`, and its alerts name the Backfill check. It never
re-grabs, see `regrab.regrab()`, and it posts its alerts. Its conversion keeps no original, as an
import's does, and the proof still gates the swap. Right after the swap the job in the queue takes
the new path, file id and key. So a run after a kill starts from the new file. It follows a rename or
a move and drops a file that is gone or replaced, as a deep analysis does, see `follow()`.

A job yields to an import that waits before a conversion and before a remux, see `process.Ctx`.
After a conversion or a repair of its file the job yields no more, see `process.refresh()`. A yield
then would lose the post and the decision line of the change. So with `HOOK_WORKERS=1` and `BACKGROUND_WORKERS=1` an import
waits until the running job ends or reaches its first conversion or remux. The checks of the job run
under an import's time limit. A conversion or a remux that started runs to its end. With job
processes an import never waits for such a job. When every place is taken and no running job of the background queue
can free one, the import starts in one more process, up to `HOOK_WORKERS` imports, see `runner.next_jobs()`. Any other
job of the background queue can free its place, and so can a job that yields already. The import waits only while
the job holds the file lock exclusive.
The job holds it for the write stage and for the swap of a conversion, see "Write stage". Before a header repair that
the checks need first, the job takes it and runs its checks again.

Then the job queues the burn-in job, the whole-file check and the deep analysis as `run_job()` does, see
`queue_after()`. It queues none when an import of the path queued its own while the job ran. The burn-in job and the
whole-file check carry `check` in their grab fields, so they never re-grab either. A full backfill reads every file
once more for the whole-file check. The deep analysis names the job in `from`,
so it waits behind every job a backfill queued. A file whose saved deep analysis still holds gets
none, see `subtitles.sub_cached()`. The alerts the job held for it count as checked again. With
`DISCORD_POSTS=all` the changes they name still post. The import check keeps that saved result,
because a result that asks for nothing never replaces a deeper one that still holds, see
`subtitles.sub_cache()`. A flag edit that a hard link or a read-only file refuses asks for nothing.
So such a file gets one deep analysis. `--drop-queued` drops the jobs a backfill queued, and the
jobs they queued stay.

**The flag backfill.** A plan option runs the flag backfill, which fixes the default tracks and
language tags. Its `--apply` posts the alerts as a job a backfill queued does, through
`logs.alert_findings()`. A problem posts once per file, kind and size. With `DISCORD_POSTS=all` each change posts
too. A dry run posts nothing. It runs at nice 19 and idle I/O, with the hook's file lock
for each file. It runs language detection, the metadata checks and the header check like the hook,
but it never re-grabs. It reads each `.mkv` file's own tracks, because the app's stored media info goes stale. It
takes a file with two or more audio tracks, any subtitle, a tag to fix or another container inside.

A dry run reads `SCAN_WORKERS` (1) files at a time, and `--workers N` wins. The file server sets the
pace, so raise `SCAN_WORKERS` until the server is busy. An apply edits one file at a time, so `--limit` and `--canary` stay exact. Run a
long pass with `setsid nohup` and a log file. Run a large backfill as a dry run, an audit of the
plans, a canary, and then the rest.

```
arr-media-guard --backfill radarr --plan-out /config/plans/radarr.jsonl                      # dry run
arr-media-guard --audit radarr --plan-from /config/plans/radarr.jsonl --post                 # one summary of the problems
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl --canary 20 # a sample
arr-media-guard --audit radarr --since 2026-01-01T10:00 --post                               # the open problems since the canary
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl             # the rest
```

`--plan-out` makes its folder. With no instance it writes the plans of every instance into one file.
`--plan-from` limits an apply to the files the dry run planned a change for, and each instance takes
its own rows. It reads them all before `--plan-out` writes, so both may name one file. Each file is
planned again before its edit, so a changed file gets the current plan. `--canary N` takes N files of
each instance, one file of each class in turn. `--only-undecided` takes the undecided files, so hearing can decide them without a
probe of the whole library.

**Queued plans.** `--background` puts each file of a plan in the background queue with its plan line, see
`cli.plan_queue()` and `cli.check_queue()`. A queued plan writes nothing the user did not audit. The worker plans the
file again in a dry pass first, see `cli.planned_file()`, so most refusals come before any write. The language
hearing, the time limits and TMDB can give the apply pass another answer than the dry pass, so the apply compares
again at each write.

One function owns every write of a queued job, the write gate `gate.allow()`. Each write of `process.py` and of the
burn-in job runs through it: the conversion, a header repair, the subtitle remux, a flag edit with its repairs inside
`process.edit()`, the edit that follows a remux, the sidecar changes, the burn-in mute and the move back of the extras
that the job's own conversion hid. `allow()` compares what the
write writes with the plan line, and runs the write only when they agree. A difference raises `gate.PlanRefused`
before the write, and `process.process()` ends the run with `process.refuse()`. The line has the outcome `plan_refused`,
`plan_diff` and no class, so the audit never takes it as the result of the file. It keeps the writes the run made
first, with their rescan. `process.act()` compares all that the run will write, see `process.writes()`, before the
first write of the one write. The conversion compares its digest inside `convert.convert()`, after its subtitle check
and before its first sidecar move, through `gate.opened()`. The digest holds the result, the broken tracks, the
verdicts and fixes of the subtitle check, and each sidecar it muxes, by name, language, flags, size and mtime. After a
remux the run plans the new file again, see `process.write()`. Its edit must equal the edits of the plan line on the
tracks the new file holds, by track UID, see `process.replanned()`. So the remux keeps the edits of the line, and an
edit of the replan's own refuses.

A guard proves that no write goes around the gate. `gate.armed()` installs one audit hook in the process, see
`sys.addaudithook()`, and arms it on the job's file while the job runs: through both passes of `cli.planned_file()`,
and through a deep analysis or a burn-in job that holds a plan. The guard watches the file and its sidecars: each
name in its folder that is its stem, or its stem, a dot and more. It sees an open for write, a rename or replace, a
remove, a link, a truncate, a shutil move or copy, and a start of mkvpropedit, mkvmerge with `-o`, mkvextract or
ffmpeg that writes such a name. Outside a window of `allow()` it raises `gate.Escaped` before the write runs. The
guard keeps the first one, and `armed()` raises it again at the end of the job, so a caller that catches it still
fails the job. The job's line then has the outcome `error`. The dry pass writes nothing, so its `allow()` opens no
window, and the guard fails each write of that pass. The hook does nothing while no job is armed. AMG's state
folder, its temp files and the kept originals are outside the watched names. The launch guard of `ff.py` is a second
audit hook, see "Time reads". A start of a tool must pass both. The launch guard judges the program, and this guard
judges the files it writes. Python calls no later hook after one raises, so a refused start counts once.

Two exceptions hold, and the gate knows them. No dry run sees the file that a conversion makes. So after the job's
own conversion, `process.conversion()` sets `ctx.row` to None, and the rest of the job runs as `--check --apply` runs
it. The gate then compares nothing, and each write still goes through it. The extras that another run's conversion
hid stay hidden, as the plan line holds no move of them. The next import or `--check --apply` of the file moves them
back, and so does a `--convert` apply, see `convert.pending_recover()`. The job stores the key of the new file as
`converted`, and each save after a later write of its own moves it on, so a run after a stop goes on from the file the
job left. The jobs that follow such a file run as after an import, with no plan and no guard. The other jobs that a
job of `--check` queues follow the plan line. A dry `--check` with `--plan-out` keeps the plans of its deep analysis and
its burned-in subtitle check in `deep` and `burn_in`, see `cli.check()`. The burned-in subtitle check of the dry run
reads the flags that the import's planned edits leave, see `remux.planned_state()`: the audio that then plays first
and the subtitles that are then on. The job runs it after those edits.
`runner.queue_after()` gives each job its plan in `audited`. A field that a deep analysis leaves empty never refuses,
as it writes nothing there. So a deep analysis that finds the import's fix made already ends with no change. The
whole-file check of a job of `--check` only says what a re-grab would do, so it needs no plan.

A stop after a write of the job's own leaves the key of the file in the job, with the reason codes of the writes, see
`process.stop_line()`. The next run of a job that did not convert then refuses, and its line says that the job wrote
the file before the stop. The rescan that the stop held goes out then.

## Audit

`--audit` never edits. It prints one summary and, with `--post`, sends it as one embed when it found a problem. With
`--plan-from` it groups a dry run's plans by policy path and rule, counts the undecided and dropped
plans, and checks the invariants again. A plan with no undecided, dropped or skipped file and no broken invariant
posts nothing. With `--since 24h` or `--since <ISO date>` it reads the
decision log. It expects no further edit in each edit's `recheck`, and it probes an edit logged
without one.

Schedule `arr-media-guard --audit <app> --since 24h --post` each night for each app, with a systemd
timer or cron. Its post, "Open problems: 2 files · Radarr <INSTANCE>", lists only the files that still have a problem. A problem is
a re-check that still plans an edit, an undecided or dropped plan, a broken invariant, a failed or skipped repair or
conversion, a probe that failed, or an error. The audit posts nothing when no file has an open problem.

An error counts while it is the latest line of its check for the file, by the line's source. A later line of the same
check that ended otherwise clears it. A line of another check leaves it open, because each check runs its own steps.
The post names the check as an alert does. A time limit reads "the backfill check ran out of time", and any other
error gives its exception, cut to 100 characters.

Each file counts once, by its latest result. A result is a decision line of an import, `--check`, a backfill or a job
a backfill queued. These runs decide the flags and the container. The deep analysis and a recheck keep the flags. The
burned-in subtitle check changes only the English subtitle flags. The whole-file check line has no class, so it is
never a result. Their lines never hide an earlier problem. `--check` runs the burned-in subtitle check, the whole-file
check and the deep analysis after its own checks, and their lines name `check` as the source. The audit tells them apart by
the class of the burn-in line and by `"deep": true` on the deep analysis line. A problem of the check after an edit
counts until a later result. The post leaves out these files and problems:

- A file that is gone, as after a conversion to a new name. A file that an upgrade replaced at the same path has the
  import's new line as its latest result. The store drops the size and time of a decision line, so the audit cannot
  tell a replaced file in another way.
- A failed conversion or file repair whose own alert went out. Its line says so in `alert_kinds` and `alert_result`,
  with `sent` or `already sent`. A failure whose alert failed or never posted stays in the post. A failure in
  `--convert` or `--sub-check` posts no alert, so it stays too.

The post has one line per file, with its problems in plain words. The name of each file links to its page in the app,
as in an alert. Its footer names TMDB only when TMDB had trouble that day. The terminal prints the same files. The
syslog summary line goes out every night. It counts each problem of each decision line of the day, also the problems
the post leaves out. It also counts the files with each RFC 9559 fault kind, by the latest line of each file in the
window that ran the header check, scans and dry runs included, as `rfc_version=3`. A repair in the run that found the
faults clears them. A `refused` key counts the time reads that `ff.py` refused in the window, see "Time reads".
The terminal prints the same counts. The audit also removes kept originals past their age and writes the policy
status, see "Status file".

`--check-audio` and `--check-video` scan every file of the app the way the worker checks an import.
A scan never deletes, edits or re-grabs. It checks `SCAN_WORKERS` files at a time, and each worker
pauses 2 seconds before a file. It keeps its place in the state store, so it resumes over days, and
`--restart` starts a new pass. Problems go to a list file, `STATE_DIR/<kind>-scan-<app>.txt`. A video scan lists a header
issue, except padding alone, see "RFC 9559 check". A run that finds a problem
posts one summary embed, and a clean run posts none. On NFS, idle I/O
priority has no effect on the file server, so the worker count and the pause are the throttles.

## Reverse an edit

Each edit logs `undo`, the complete mkvpropedit command with the old values, before mkvpropedit
runs. The selectors are track UIDs, so they hold when the track order changed. monitoring.md shows how
to run the undo of a file's last edit. A later import of the same item runs the hook again. Delete the connection in the app to stop that.
A remux that carries the edits, see "Write stage", logs the same `undo` with its record, after the swap. The remux keeps
every track UID, so the command holds for the new file. The kept original undoes the whole remux, and each earlier
write of the hook since it was kept, see "Kept originals".

## Write stage

The write stage makes the run's planned changes in one write, after every check and decision, see `process.write()`.
The checks and the decision read the file as found, with two exceptions that write before them, because the checks
cannot read through what they fix. The conversion comes first, as the checks read Matroska only. A header issue the
checks cannot read through is repaired next: no usable Cues, a header duration far from the streams, a Segment size
left by the hook's failed edit, a subtitle event far past the streams, and enclosed zeros, see "Header repair". So a
run can write its file more than once, for example a header cut first and a subtitle retime at the write stage. It
keeps one original, the file before its first write, see "Kept originals".

The write runs after the re-grab decision, under the exclusive file lock that the flag edit takes. A planned remux
runs once. A subtitle fix plans the subtitle remux, which repairs a layout or tail issue of the header in the same
run. A header issue alone plans the header repair. The remux carries the flag and language edits of the
tracks it keeps, as mkvmerge track options. Its proof holds each track of the new file to the planned state, the
properties of the original with the edits applied. A track the remux takes out counts for the decision as a track that
does not match, so it gets no flag. A read-only file gets no edits in the remux, so its record still says
`read_only`.

After the remux, the run probes the new file and plans it again, as the file reads with the carried edits taken back.
That is the plan 2.18.3 made after its remux, so the record keeps its decision and reasons. Its edits are the change
from the original. So they can name a BCP 47 tag of an edited track that 2.18.3 left out, because mkvmerge wrote that
tag in its remux. When the plan takes back every carried edit, the record says no change, as in 2.18.3. mkvmerge
writes statistics tags into a file another muxer wrote, and the rules read them. A removed track changes the places
and can change a choice. So the plan can differ from the one the remux carried. mkvpropedit
then makes its edits in the same run, and takes back a carried edit it no longer asks for. `replan_edits` in the
record counts them. A repaired garbled track whose default flag goes, see "Garbled subtitle repair", keeps that edit
in its repair.

When the remux fails or cannot run, the original stays. mkvpropedit then makes the edits of the plan as it was before
the remux: a track that does not match loses its default and forced flags. A dry run records those edits. A run tries
no second remux after a failed one, except the layout repair below after a failed subtitle remux. A remux skipped for
a hard link, the size cap or low space is no failure, so the guards below still run.

With no remux planned, mkvpropedit makes the edits. Two guards keep the Matroska layout whole for every caller of
`process.edit()`: an import, a backfill, `--check`, a recheck, the deep analysis and the burn-in mute.
`HEADER_REPAIR=false` turns both off.

- **Bytes past the Segment end.** mkvpropedit grows Tracks into the first Void that holds it. With none, it writes the
  grown Tracks at the end of the file and extends the Segment over the bytes past its end. Those bytes then sit inside
  the Segment where no element starts, and mkvmerge warns on them. Bytes past the Segment end (`edit_tail`) are a
  header issue at any count, so the header repair carries the edits, see "RFC 9559 check". This guard holds for a
  caller with no header check, and when that repair was blocked or failed. The edits then go into one header repair in
  place of mkvpropedit, after the video check looks for real damage. When the remux must not run (`edit_blocked`:
  another Matroska file past the Segment end, or a Segment that may be cut, or a block of the video check), or a remux
  of the file failed earlier in the run, the run writes nothing. The flags stay, the outcome is `edit_failed`, and one
  alert says why. `edit_blocked` stops a subtitle remux too, because it drops those bytes as well. 2.18.4 (docket 54)
  kept the bytes of a file with no flag edit. The owner changed that on 2026-10-20.
- **A layout the edit broke.** After mkvpropedit, `checks.layout_fault()` reads the new layout. A broken one gets the
  header repair at once, after the video check of the edited file. The repair runs after the signal block of
  mkvpropedit ends, so a stop removes the temp file of the remux and leaves the edited file. The run then ends with its
  decision line, see "Crashes and stops". A header repair that failed or was stopped earlier in the run blocks it.

After a repair inside the flag edit, the run probes the new file, so the metadata checks and the quick burned-in
subtitle check read it. A file the app removed or replaced at the path meanwhile gets none of them, as in 2.18.3.

A zero run that an older flag edit left inside the Segment (`enclosed`) is a header issue, which the import repairs
before its checks, see "Header repair". A caller of `process.edit()` that ran no header check, as the burn-in mute,
gives such a file its edits in the repair.

The run sends one app rescan after its last write. Each part of the record that names a remux holds its answer in
`rescan`. Up to 2.18.3, a subtitle remux or a header repair in a job under the shared lock ran the whole job again
under the exclusive lock, and a flag edit could follow a remux in the same run.

## Conversion

Every video file that is not Matroska becomes `<base>.mkv`, and so does a `.mkv` file that holds
another container. `--backfill <app> --convert` converts the library. The hook converts an import
only with `CONVERT=true`, except a `.mkv` file with another container, which always converts. The
log calls a conversion a repack. A conversion keeps no original, except a forced one, one that leaves out a
track that does not match, and one of `--check`.
A conversion of `--check` also keeps the sidecars that the new file holds, in the stamp folder of the
original. It proves every stream first,
and the app must list the new file before the original goes. A `--check` run on a file no app lists
asks no app. It writes the new file beside the original, which stays with its sidecars. An `.mkv` file
that holds another container converts under its own name, so its original goes to the kept originals.

**Skips.** A skip writes nothing, and the file goes on `STATE_DIR/convert-<app>.txt`.

- a hardlinked file, because the rename would split the link
- a file over `REPACK_MAX_GB` (30), a file with less than twice its size free, or a taken new name
- a file that is not the item's listed file inside the item's folder. The app moves and renames a
  file from outside the folder, and a bonus file could pass as an upgrade.
- a Sonarr name, or an extra's name, that the parse API reads as other episodes, because the rescan
  links extras by their parsed episodes
- a new name that loses a custom format that scores, a file ffprobe cannot read, or an empty
  caption track
- a conversion of `--check` with no place to keep the original. A dry run of `--check` and a file
  no app lists never go on the list.

**Refusals before the remux.** An MP4 VobSub track with no picture size, a width or a height of 0 in its track header,
fails the conversion before the remux, in a dry run too, see `convert.vobsub_unsized()`. mkvmerge writes the size of the
MP4 track header into the VobSub header of the MKV, and ffmpeg opens no decoder for such a size. An apply posts the
alert of the failed conversion, and a dry run posts none. The original stays. A VobSub track with a size goes to the
remux. mkvmerge 102 sets the stop time of each VobSub cue from the length of its MP4 sample. With a gap after a cue,
ffmpeg's MP4 muxer writes a sample that lasts until the next cue. Each such cue of the MKV would show until the next
one, and the proof refuses. Cues back to back convert. A made-up track whose muxer fills each gap with an empty sample
converts too, the shape that libhb/muxavformat.c writes. No HandBrake file was measured.

**The remux.** A `.srt` beside the video that starts with its base name is muxed in, with its
language, forced and hearing-impaired flags from the name. Its text can overrule the name's language,
and a name with no language takes the text's. A sidecar in a legacy codepage goes in decoded right.
A sidecar whose charset is unknown stays beside the file as it is, see "Subtitle text". AMG reads the cues of a
sidecar as mkvmerge 102 reads them, see `decide.srt_read()`. mkvmerge writes no cue with empty text, and the proof leaves
such a cue out too. mkvmerge reads some sidecars otherwise than a player shows them, see `proof.misread()`. It may not
identify the file as SubRip, see `decide.srt_probe()`, and then the remux fails. mkvmerge identifies a sidecar named
`.srt` that holds ASS or SSA text as SSA, see `decide.ass_text()`. The conversion leaves it out with the reason "it
holds ASS or SSA text under a .srt name", also when AMG can't tell its charset. mkvmerge also stops reading at a
number line that no timing line follows. It reads a number and a timing line after a text line with no blank line as
text of the cue before, where a player shows a cue of its own. It drops a cue with text that ends before it starts,
which ffmpeg and mpv show up to the next cue. It also drops a cue of length 0, which no player shows. mpv 0.40 showed
the same cues from such a sidecar and from the new MKV. So a cue of length 0 counts as no cue, in `misread()` and in
the sidecar proof, and the conversion muxes such a sidecar.
A sidecar that mkvmerge misreads stays beside the new file byte for byte. `repack.sidecars_left` says why, as
"Movie.es.srt: mkvmerge drops 1 of its cues", and the "Subtitles left out of the MKV" notice names it, see
"Unreadable subtitles".
The proof still refuses a stop and a dropped cue, as a guard. A sidecar that ends past the video, or
has cues out of order, is timed for another cut and stays beside the file. `repack.sidecars_left` names each sidecar
that stays and why. `mkvmerge
--disable-lacing --track-order` writes the temp file into `.<NAME>-convert` beside the video. The apps' disk
scan and Plex skip a hidden folder, so they never import a partial file. A hidden file beside the
video is not enough. The app's scan takes it as an extra of the item and can rename it or send it to
the recycle bin. A swap once hid it with the extras, and the link to the new name failed. An
mkvmerge error fails the conversion. mkvmerge exits 1 on warnings alone, and then the proof decides,
because a warning can be harmless, such as zero bytes it skips at an audio end. ASF and WMV go through
`ffmpeg -c copy -copyinkf`, because mkvmerge cannot read them, and any ffmpeg message fails them.
`-copyinkf` keeps the frames before the first keyframe. A c608 caption track becomes an English
SubRip track, because mkvmerge drops it. ffmpeg's 608 decoder gives each cue its start and end. It reads with
`-copyts`, so each time is the one the file presents. Each time then moves by `start_shift()`, as mkvmerge moves the
tracks it keeps. Each cue then sits with the video frame it showed with. ffmpeg's own start of the file counts a
caption track that starts less than 1 s before the video. Before 2.18.6 the cues sat that much late.

**Broken timed text.** MP4 timed text must be UTF-8. Some files hold a track in an old codepage, such as Persian in
cp1256, and every player shows it as garbage. mkvmerge's MP4 reader, in 92 and 102, writes U+FFFD for each byte it
cannot read. It ignores `--sub-charset`. So `broken_tracks()` reads the packets of each timed-text track with `-c copy`
and takes the text bytes after each 2-byte length. A track with any packet whose text is not valid UTF-8 is *broken*.
One stray byte in an English UTF-8 track is enough. AMG does not repair a broken track, because each automatic repair
it tried had a case that wrote wrong text.

The remux removes a broken track, and the proof leaves it out, as for a track the subtitle match drops. The subtitle
match does not read it. Its bytes go beside the new file, see "Unreadable subtitles". The file is SubRip. Its number
lines, time lines and blank lines are ASCII, and each cue holds the text bytes of its packet as they are. The times
follow the new file. mkvmerge starts an MP4 at the earliest packet of the tracks it keeps, before 0 or after it. An AAC
priming edit puts the first audio packets before 0. A removed track no longer counts, so a video that starts after 0
then starts at 0. The times move by the same amount, see `start_shift()`. When no name is free, the conversion is
skipped as `repack_name_taken`. A stranded conversion keeps the file beside the new file. A dry run writes nothing,
and its result names each file. The decision line names each broken track in `repack.broken`, by its place in the
original.

An AVI stream header can hold `dwStart`, the time the stream starts after the start of the file.
ffmpeg honours it, and mkvmerge starts every track at 0. A video that starts 2 frames late would then
play 83 ms early against the audio. So for an AVI, ffprobe reads the first packet of each video and
audio stream, and mkvmerge gets each start of 1 ms to 10 seconds back with `--sync`. When one stream
starts later than that, no stream gets `--sync`. mkvmerge then starts every track at 0 as before
2.11.2, and the proof decides. The first packet decides, because ffprobe's stream start time can say
0 for such a stream. The decision line names each start in `repack.start_sync`. Other containers get
no `--sync` for a late start, because mkvmerge reads an MP4 edit list and the times of a TS itself. The proof still
checks every start.

**An MP4 video in its own input.** mkvmerge hands the frames of each track to the code that writes that track, and
this code can hold frames back. When one track of an input reads its last packet, mkvmerge tells the code of every
track of that input to write out the frames it holds. For HEVC and MPEG-1/2 that works only the first time. So when
another track ends first, such as a subtitle track or an audio track, the video loses frames at its end. A GOP is the
run of frames from one keyframe up to the next. HEVC loses every frame after its last keyframe. It loses that keyframe
too when the other track ends before the GOP that leads up to it. MPEG-1/2 loses more. mkvmerge 92 and 102 drop them
with no warning, and the proof refuses the new file. This is mkvmerge issue 6320,
https://codeberg.org/mbunkus/mkvtoolnix/issues/6320. So each video track of an MP4 with a video codec of `LAST_GOP`
comes from its own input of the file. The other tracks come from one more input with `-D`, and only that input gives
the chapters, the tags and the attachments. mkvmerge starts each input at its own earliest packet. So `split_sync()`
gives the tracks of each input a `--sync`. It is the time from the earliest packet of all kept tracks to the earliest
packet of the input, see `start_shift()`. A dropped track and a broken track are not kept. They count for no start,
and no input reads them. The sample tables give each start, see "Time reads", as mkvmerge presents the edit list. A
file keeps one input when a kept track has no sample. It keeps one input too when the edit list cuts a sample before
its start, because mkvmerge keeps and times such a sample by its own rules. In a file cut with `ffmpeg -ss`, ffprobe's
start of the audio was 0.3 s off. Audio samples that end at 0 s or before, and start less than 1 s before 0, are the
exception. An AAC priming edit gives them, and mkvmerge keeps them on the edit's line. An empty edit before them moves
them past 0, and mkvmerge then drops them, so that file keeps one input. `--sync` takes whole milliseconds, so
a packet can move 1 ms against a one-input remux. The decision line names each `--sync` in `repack.split_sync`. H.264,
MPEG-4 part 2, AV1 and VP9 lose no frame, and their files keep one input. The test
`test_mkvmerge_still_loses_the_last_gop_of_an_hevc_mp4` is the signal to remove the split. It fails when a mkvmerge
with issue 6320 fixed keeps those frames.

**ffmpeg's MP4 start.** AMG's reference timeline for an MP4 is the one ffmpeg-based players (Plex, mpv, Kodi) play.
An H.264 or HEVC encoder delays the first frame by its B-frame reorder. When the video's edit list starts at media
time 0, the edit keeps that delay, and ISO/IEC 14496-12 presents the video 2 frames late (83 ms at 23.976 fps).
mkvmerge 102 writes it so. ffmpeg moves the track earlier by that gap ("Offset DTS by N to make first pts zero").
ffmpeg turns the rule off for every trak after the first trak with no samples, and the reader does the same.
`mp4.ffmpeg_lead()` reads that rule from the sample tables, and `mp4_sync()` gives mkvmerge the move with `--sync`,
toward 0 in whole milliseconds (`proof.start_plan()`). mkvmerge drops a frame it would time before 0, so the move
never rounds past the gap, and the video lands up to 1 ms later than ffmpeg plays it. The decision line names the
move in `start_sync` with its rule. The proof expects that exact move and refuses any other. A tester's film read
"stream audio 1 starts -0.083 s from the video in the new file, +0.000 s in the original" in 2.18.5.

**Kept audio times.** mkvmerge 102 times an audio frame on its own where its time is not later than the time of the
frame before it. It times that frame one frame after the frame before, and plays the rest of the track on. An MP4 can
store two AAC samples at one time, or three for an iTunes priming run, and a ctts can step back. A TS PES can take the
PTS of the next one. mpv plays each audio frame at its stored time. So such a copy plays the audio late. At 32 kHz
it plays 32 ms late to the end, and after a step back of 150 ms it plays 150 ms late. So `kept_times()` gives each such track a v4 timestamps
file (`--timestamps`) of the times the reader gives, see "Time reads". Each line holds the time of a frame with data,
in file order, less the file start. The file start is the earliest time of the tracks the remux keeps, which mkvmerge
moves to 0. For an MP4 it is `start_shift()`, with each video track before ffmpeg's start rule. For a TS it is the
earliest time of a kept PID. mkvmerge writes each line as it stands. It adds no input start and no `--sync`, so a line
holds in the input of the other tracks of a split too. The proof then pairs each frame with its stored time within
the slack. A PCM track, a track with an empty frame or a time that is not known, a read that fails and a start that
is not known get no file. So does a track with a cut sample after an empty edit, because mkvmerge drops that sample
and the lines would then not pair with its frames. Those tracks keep mkvmerge's timing, and the proof decides. The
decision line names each track with a file in `repack.kept_times`. Of 441 MP4 and QuickTime files of a measured
library, 8 hold such a track, each an iTunes priming run of three samples at one time.

**Blu-ray TrueHD.** A Blu-ray TrueHD PID carries an AC-3 core in PES of its own. ffmpeg names the core as a second
stream of the PID, also when the PID holds no core PES. mkvmerge 102 writes the core as an AC-3 track after the TrueHD
only when its probe finds core frames. So the proof pairs a core that mkvmerge -J does not list with no stream of the
new file (`proof.lone_cores()`). The decision line names such a core in `repack.cores_left_out`. Such a core must hold no
frame. A core whose first PES comes after mkvmerge's probe holds frames, and mkvmerge leaves it out. The proof refuses
that file, because the copy would lose the core. Up to 2.18.5 every TrueHD M2TS with no core PES refused with "the new
file holds 1 audio streams, not 2".

**The proof.** No stream is decoded. ffmpeg reads both files at once with `-c copy -copyinkf
-copyts -f framemd5` and pairs the streams of each kind. `-copyinkf` keeps the frames before the
first keyframe, which a copy drops by default. Without it, a new file that lost them would pass. A stream must keep its codec, its count of packets
with data, and a digest over every packet. Where mkvmerge changes packets on purpose, a bitstream
filter brings both sides to the same bytes (`PROOF_BSF`). mkvmerge drops the filler data units of H.264,
which carry no picture, so the filter takes them out of both files. Each time comes from the reader of its
container, see "Time reads", and each frame and each stream's start against the video's may move only the rounding
of the two files (`proof.mkv_slack()`). A copy that moves one track by 2 ms refuses. The video and audio must end
within 1 second. A source with no reader (AVI, ASF, MPEG-PS) reads its times through ffmpeg: each stream must start
within 0.05 seconds of where it started (`SYNC`), and each packet time may move 2 ms at most (`TIME_SLACK`), because
a stream whose times jump back can pass the digest and still play late. Times compare in whole microseconds. A
subtitle remux counts each subtitle time from 0, and any other proof from the video's start.
When the times fail, the refusal says where the MKV would play out of sync. It says "from 2:00 to
the end" or "in 2 places between 0:10 and 1:49", for example. The times count from the start of the MKV, as a
player shows them. Then it names the stream, how far it moved, and the time of the first packet that
moved.
A conversion writes each frame in its own block (`--disable-lacing`), so the new file stores a time for every frame. A
Matroska source, such as a WebM, gives an audio track that mkvmerge would time on its own its planned times, see
"Planned audio times". MP4 timed text becomes SubRip, and its cues must match.

**Where a packet sits.** A player reads the file in order. A packet stored minutes after the packets of its time never
plays at its time. The ffmpeg subtitle remux read each rewritten track from its own input, and with a sparse copied
track ffmpeg held that input back and wrote its packets in bursts minutes late. The proof reads the file position of
each packet from the reader of its container, see "Time reads", and from an ffprobe read of a file with no reader. A packet's lag is how far its time trails the latest time of the packets
before it in the file. A stream fails when its worst lag in the new file is over 1 second (`LAG_SLACK`) and over the
worst lag of its paired stream in the original by more than 1 ms, both in whole ms (`proof.lag_over()`). A new file
with video stores whole ms. A TS or MP4 original stores 90 kHz ticks, so the copy of its step back of 1.0004 s steps
back 1.001 s. So a stream may sit up to 1 second behind, or 1 ms further behind than in the original. A sidecar has no
original stream, so 1 second alone bounds it. The proof, the header repair and the planned audio times all take this rule. A PGS track
can store a display set right after its own display set of a later time. A Blu-ray file measured on 2026-10-24 holds
three such tracks. Each stores a display set at 4303.383 s right after its display set at 4304.384 s, a step back of
1.001 s in the track's own order. mkvmerge keeps the packet order of each track, so 1.001 s is the lowest lag any
writer can give them. The original holds 1.044 s, because an audio packet at 4304.427 s sits before them. B-frames
give video a lag of a few frames in any file. The refusal names the
stream and both lags, and each proof entry keeps them in `lag`, [original, new file]. A new Matroska file must also
pass the layout check of "Header repair" before any packet is read.

**Laced audio.** A Matroska block can hold several audio frames, a lace, and it stores one time for all of them.
ffmpeg times the other frames from durations floored to the millisecond. So when both files are Matroska, the proof
reads every time from the blocks, see "Frame times", and the frames of the two files pair in file order. mkvmerge
laces the frames of a Lavf file, and `--disable-lacing` takes a lace apart. Both keep every stored and defined time
and pass. A block moved 3 ms, a lost frame and a drift still fail. A frame whose time is not known drops out of the
check, and the proof entry counts it in `times.unknown`. Its block's frame 0 still counts.

Matroska stores no decode times, so ffmpeg guesses them during its read. A frame stored far ahead of
the frames it displays after breaks the guess, and ffmpeg's muxer then moves a few packet times of
the read. The muxer also sets a time that a file stores lower than the time before it in the file to that time.
Some remuxes store a PGS display set before the set it follows, so ffmpeg's read of both files moved the same
packets, and a copy that moved them passed. The walk of the blocks reads neither change. When only the new file is
Matroska and the time check fails, the times its blocks store decide. The proof entry names that second check in
`times.reread`. Stored times that moved still fail. The walk and ffmpeg must count the same frames of each stream,
else the proof refuses.

The proof allows a cut last frame and a cut first audio frame that mkvmerge drops, because a cut
frame cannot decode. After a cut first frame, the second packet must keep its time within 2 ms
(`TIME_SLACK`). mkvmerge drops an MP4's cut first audio frame after an empty edit and keeps it with
none, so a `--sync` of `split_sync()` from its time plays the audio a frame early. The proof refuses
that file. mkvmerge may also keep only the tail of a cut first MP3 or MP2 frame. That
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
zeros. The audio may also lose a short lead at its start, whatever its packets hold, when the lost
packets span at most 0.1 seconds of the original (`LEAD_LOSS`). An AVI can start its MP3 stream with a
silent frame, a few stray bytes and cut frames, and mkvmerge drops them all. After a short lead, the
first kept packet must keep its time within 2 ms (`TIME_SLACK`). A VBR header in an AVI times each
audio chunk as one frame. With one, mkvmerge can drop the lead and play the rest of the stream a frame
or two early. The start check allows 0.05 seconds, and an AVI keeps no audio times to check, so only
this rule refuses it. After other lost packets, the kept audio keeps the start check, and its times
where the container keeps them. The proof entry names each lost packet in
`dropped_start` or `dropped_end`, with its time, size and kind, `lead` for a short lead.

Two more differences pass, each with its own check:

- An HEVC codec header can hold units that mkvmerge copies into packet 0, such as an SEI. The proof
  filter takes out only the parameter sets. When only packet 0 differs, one packet of each file is
  read. It passes when the new packet 0 holds the original's and units of the header, byte for byte.
  The entry names them in `header_units`.
  mkvmerge also puts the entry point of a VC-1 header in front of packet 0, and the same check passes it.
  From an MP4 or MOV, mkvmerge puts the whole codec header of MPEG-4 part 2 in front of packet 0, also
  when packet 0 holds it already. It passes when the new packet 0 is that header and then the
  original's packet 0, byte for byte. From Matroska and AVI, mkvmerge keeps packet 0 as it is.
- An MP4 timed-text cue can start and end at one time, beside another cue at that start. mkvmerge
  gives it a length. It passes when it keeps its start and text and ends at or before the next
  cue. The entry names it in `zero_length`.

Any other lost packet fails, and the file keeps its original.

**AAC priming with no edit list.** Some MP4 files, such as one iTunes purchase, store the AAC encoder priming as
leading packets of zero length, and have no edit list. Their first three packets then share one time. mkvmerge 102
spaces them one frame apart and plays every later packet on, two frames (about 44 ms) late. Up to 2.18.8 the proof
refused such a conversion, and the file stayed MP4. Now the conversion keeps each stored time, see "Kept audio
times", and the copy plays as the original.

**Force a conversion.** Some refusals are safe, and only a person can tell. One example is an MP4
whose edit list hides the last frames of a still picture, which mkvmerge keeps. `--backfill <app>
--convert --apply --force-convert PATH [PATH ...]` takes only the listed files. The force is bound
to the refusal a person saw. A file converts when the proof refuses it with the same text as the
last refusal the decision log holds for its path. Another refusal, or none in the log, is not
forced, and the run reports it. Every other step runs as normal: the remux, the swap, the app's
import, the extras and the checks after them. The original is kept in `.<NAME>-originals` for `KEEP_ORIGINALS_DAYS`, as a
header repair keeps it, so one move undoes the conversion. See "Kept originals" for the hard link and its copy fallback. The option needs `KEEP_ORIGINALS_DAYS`
above 0. The decision line names the refusal in `repack.forced`. A listed path that is not in the run's work list is reported and skipped. The hook
never forces a conversion.

A listed Sonarr file also skips `parse_refuses()` for its own new name. Scene numbering can put specials at the start of
a season, and an alias can match another show, so the parse can read a right name as other
episodes. The ManualImport names the item's own episodes by id, so the link stays right. The parse
result is fixed, so this needs no logged refusal. Every other check still runs, and the proof must
still pass unless its refusal is logged. The decision line names the parse result in
`repack.forced_name`, and the original is kept as for a forced proof. The extras keep the check. They go back by a rescan, which links them by their parsed
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
refuses the conversion. The app scores the stored file on its record's quality, languages and group, which
the import carries. Only a Release Title condition reads a name, the title or the file name. So the hook reads
the app's custom formats and matches each one's Release Title regexes on the old and the new names, as the app
would. A format counts as lost only when the file's record lists it now, and as gained only when it does not.
For a regex that Python cannot compile or reads another way than .NET, the parse API decides. The parse API
alone is no guide, because it cuts the extension before it matches and reads the quality from the name. A name
with no quality takes one from its extension. `.mp4` parses as SDTV on both apps, and `.mkv` as WEBDL-720p on
Radarr and HDTV-720p on Sonarr. The skip names the lost and gained formats with their scores first, and
`convert-<app>.txt` keeps the whole text. On Radarr the hook
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
  track, or "1 byte ... which was skipped", more than `SKIP_EDGE` (5) seconds from either end of the
  file. The proof then refuses that same audio stream, for its packet count or its packet data. The
  k-th audio track of mkvmerge is the k-th audio stream of ffprobe, as the proof pairs them. A TrueHD core that
  mkvmerge leaves out has no place in that count, see "Blu-ray TrueHD".
- ffmpeg does not read the original cleanly in the proof, with "NAL unit size", "Invalid data found" or
  "partial file". A failed read of the temp file says nothing of the original. A line of a subtitle stream's
  decoder never counts (`SUB_DECODER`). ffmpeg 7 prints "Error decoding subtitles: Invalid data found" for timed
  text that is not UTF-8, while the video and the audio read. A read whose only errors are such lines is a refusal.
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
proof's filters, and ffprobe probes it. A line of a subtitle decoder is no damage there either. The proof does not run again, and its refusal stands. The
re-grab judges only the job's own file. Another file of the download shows its damage in its own
conversion, and joins the unit then. A re-grab that deletes the file ends the job. Otherwise the
original stays and gets the import's audio and video checks. The decision line has the outcome
`damaged_source`, the `regrab` code and `repack.damage`. One red "Damaged file" embed says what the
hook did.

Only a hook job re-grabs. A library backfill with `--convert` lists a damaged file in
`convert-<app>.txt`, and it never re-grabs. The re-grab needs `damage` in `REGRAB`, which is off by
default. Without it the hook checks the original again and posts "Damaged file, re-grab is off". A
file ffprobe cannot read is skipped and listed, with no "Conversion to MKV failed" alert.

## Frame times

AMG reads the time of every frame of a Matroska file from its blocks with its own parser, `matroska.frames()`. ffmpeg
estimates some frame times, so no check of two Matroska files compares ffmpeg's times. The proof, the frame count of
a header repair, the lags and the whole-file read of the subtitles take the times of the walk.

**The walk.** One pass reads the element headers, the block headers and the lace sizes of every Cluster through mmap.
Where a lace needs the codec's frame length, the walk keeps the first 16 bytes of each of its frames, and of a Vorbis
track the first byte of each unlaced block. The walk drops each page behind it, so a second read of a lace reads it
from the disk again. A 7 GB episode has a DTS track that laces every block with no DefaultDuration. Its frame times
after the walk took 11 to 15 s cold with that second read. They took 1.1 to 1.6 s with the kept bytes. A file whose
Tracks comes after its Clusters reads those bytes after the walk. The walk takes the first Info and the first Tracks,
as ffmpeg, mpv and mkvmerge read them. A second one after the Clusters does not count. The frames of a text track that zlib packs read after
the walk too. A packed text frame that unpacks to no byte holds no data, as a frame of 0 bytes does. ffmpeg, mkvextract
and the Cues read drop both, so the proof counts neither. A Cluster child or a block that does not read is damage, and
the walk names where. A Cluster that runs past the end of the file is damage too, as in a file cut at a block boundary.
So is a zero run between two Clusters. A Segment that runs past the end of the file is named after the walk. A zero run
before the first Cluster or after the last one reads, as mkvpropedit leaves one where Tracks stood. Lace sizes that run
past their block are damage, on a track with a DefaultDuration too. After any damage the walk looks for the next
Cluster id and reads on from there, as ffmpeg does. A trusted Cluster reads whole, and its Timestamp is no later than
the next Cluster that reads whole. The damaged Cluster the walk was in sets the lower bound of the search when its
Timestamp lies between the last trusted Cluster and the next Cluster that reads whole after its end. Else the last
trusted Cluster sets it, and the blocks of the damaged Cluster leave `read_on`. The walk reads on from the first
Cluster that reads whole, no earlier than the bound and no later than the next Cluster that reads whole after it. With
no Cluster that reads whole after it, the walk reads on from it only when the bytes after it read as level-1 elements
to the Segment end. So a late whole Cluster inside damage at the end of a file gives no frame. Fewer than 12 bytes at
the end of the file read as a header cut there, as in a cut download. A zero run over the header of the element after
that Cluster is a known limit. The bytes after the zeros do not read, so that Cluster leaves `read_on`. A
damaged Cluster between the damage and that Cluster keeps its blocks before its damage when its Timestamp lies between
the two. So a Cluster id inside the damaged bytes gives no frame at a wrong time, and a late Timestamp drops no good
Cluster after it. Three damaged Clusters of 5 s in a row lose 1.5 s of video, where the search that took whole Clusters
only lost 10.5 s. `tracks` keeps the frames before the first damage, and `read_on` every frame the walk read, with
`resumed`, the first frame after a resync. The proof, the header repair's frame count and the SubRip read of a time
plan take a walk with damage as no read. The whole-file subtitle read keeps the cues before the first damage, each
track marked as cut short (`subtitles.Cut`). A film of 10.9 GB with 274,346 blocks took 3 s of CPU from the page cache
and 23 s cold, against 29 to 44 s of CPU for ffprobe's packet read. The whole proof of that film and an mkvmerge copy
took 86 s and 229 s of CPU, where 2.18.5 took 107 s and 284 s. The last 4 walks stay for the file at its size and
mtime, so the proof and the header repair share one. A flag edit by mkvpropedit changes no frame, so the walk moves to
the edited file (`matroska.carry()`). Only a walk with no error of a Segment that ends at the end of the file moves,
because mkvpropedit puts bytes past the Segment end inside it.

**The head walk.** `lace_guess()` needs only the first frames, so it takes `matroska.head()`. A whole walk that the
cache holds serves. Else the walk stops after the first Cluster that gives each audio track 500 frames and the first
video track its first keyframe or 300 blocks (`remux.lace_read()`). It also stops after the first Cluster that ends
512 MiB past the Segment start (`HEAD_MAX`), and at the first damage. The tracks of a whole walk hold the frames before
the first damage, so the first frames of both walks are the same. A Tracks or Info after the Clusters, as mkvpropedit
moves them, reads where a SeekHead lists it, also through a second SeekHead. With no such entry, `lace_guess()` takes
the whole walk. The head walk never goes into the cache. On a 75 GB laced remux, `lace_guess()` read the first 29 MB
in 0.09 s cold. The whole walk took 228 s, and the ffprobe read of 2.18.5 took 0.5 s.

**The tags.** A block stores one time. A lace holds several frames, and RFC 9559 10.3.5 makes them contiguous: each
frame starts where the frame before it ends. Each frame gets a tag that says where its time comes from.

- `stored`: an unlaced block, or frame 0 of a lace. Its time is the Cluster Timestamp plus the block's relative
  time, times the TimestampScale.
- `default`: frame k of a lace on a track with a DefaultDuration, at the block time plus k DefaultDurations.
- `bitstream`: frame k of a lace on a track with no DefaultDuration, at the block time plus the lengths of the frames
  before it. AC-3, E-AC-3, DTS core and MPEG audio hold the samples and the sample rate in each frame header. AAC holds
  them in its AudioSpecificConfig.
  An Opus packet holds its frame size and its count of frames in its TOC byte (RFC 6716 3.1). A Vorbis packet lasts
  (the block size before it + its own) / 4 samples. The identification header holds the two block sizes, and the end
  of the setup header the block size of each mode, which the first byte of each packet names. The walk reads that
  byte of every Vorbis packet in order, so a short packet takes the size of the packet before it.
- `unknown`: any other frame of a lace, as the first lace of a Vorbis track, whose first packet has no known block
  before it.

A frame lasts its BlockDuration, else the DefaultDuration, else a frame of a lace its codec length. RFC 9559 ends a
block with neither at the next block of its track in time order, so only the last such block has no known end
(`matroska.rfc_ends()`). The subtitle reads take that rule, see "Subtitle match", and so does the proof of a plan of
new subtitle times: it refuses only a track whose last cue has no known end.

ffmpeg 7.1 reads frame k of a lace at the block time plus a duration floored to the millisecond, 0 to 1.3 ms early for
DTS. The proof of 2.18.0 to 2.18.5 shared the span to the next block among the frames of a lace instead. Both are
estimates. Where a film's laces overlap the next block, the share squeezed the frames, and the proof refused a remux
that held the defined times.

**The slack.** Each Matroska file rounds a time to its own TimestampScale, so `proof.mkv_slack()` takes the bound
from the two scales. mkvmerge keeps a subtitle's stored time, so only the new file rounds it: scale_b / 2. Each audio
or video block of the original rounds its time on its own, so the last frame of a lace and the next block can sit up
to scale_a apart from the DefaultDuration spacing. A copy that laces in other places times a frame from another
block of the original, and rounds once more: scale_a + scale_b / 2, 1.5 ms at the usual scale of 1 ms. An AAC track at
44.1 kHz in a 44-minute episode moved up to 1.22 ms when mkvmerge 102 laced it again, at 98 frames, and 0.46 ms with
`--disable-lacing`, which the subtitle remux and the conversion use. mkvmerge times the TrueHD frames of a copy again, 0.83 ms each, up to
1 ms off the source's times, and both are right. That is why the audio and video bound stays scale_a + scale_b / 2
for a copy with `--disable-lacing` too. The bound (scale_a + scale_b) / 2 would be 1 ms, which leaves TrueHD no margin. A time
counts from a first time, which a new file with a coarser scale rounds too, so both bounds take max(0, scale_b -
scale_a) / 2 more. A lossless copy at 0.1 ms into 1 ms moved 30 of 144 B-frame video frames 1 ms that way. All bounds
get 1 µs more, because times compare in whole microseconds. A source read through ffmpeg keeps 2 ms (`TIME_SLACK`).

**Shared times.** Two audio frames of the original may share one time, as an MP4 gives its first two AAC samples.
Each keeps that time within the slack, as any frame does. mpv plays each audio frame at its stored time. A copy that
spaces the two one frame apart played the audio 2 to 22 ms off at the first picture, and at 32 kHz 32 ms late to the
end. So a conversion keeps such times, see "Kept audio times" under "Conversion". Up to 2.18.8 such a frame could move
one frame.

**Overlapping blocks.** The walk counts, for each audio track, the blocks that start more than one tick before the end
of the block before them, and the ones that start more than a tick after it. A measured film release stores a DTS
track whose blocks overlap the lace before them 6,767 times, up to 85 ms, with 6,544 gaps. Frames of two blocks then
claim the same moment. mkvmerge 102 plays the audio on instead: each frame one frame after the one before it, until a
gap brings the stored time back. In its copy 400,866 frames sit up to 150 ms later than the original defines them,
and 308,041 keep their time.

The frames of a lace take their times from the block time and their lengths, see "The tags". In that release the
laces overlap the next block and leave gaps of the same size, so its block times step back and forth. Two parts that
mkvmerge joined into one file hold one overlap of 34 ms after a lace and no gap. mpv played a copy that closes it 31
to 34 ms late from the join to the end. The Plex simulation played both the same. A made-up copy that closes an
overlap of unlaced blocks played the audio late by the move in mpv, 22 ms after a 21 ms overlap and 150 ms after a
150 ms one. So a plan closes an overlap only after a lace, and only up to a later gap of the original, see "Planned
audio times". The proof lets an audio frame of two Matroska files move later in one case only: the new file plays on
in whole frames from the last frame that kept its time, past a block of the original that overlaps the lace before it. Its new time
is that frame's new time plus one frame for each frame between, within the slack. One frame is the DefaultDuration,
else the codec's frame length. The walk keeps the time of each block that overlaps a lace (`lapped`), and the chain
must hold one after the frame that kept its time. So a slow drift refuses, because its original has no overlap. A
frame that shares its time with the last frame of a lace starts such a block, so it may start a chain too. The move
may reach 0.2 s (`proof.CLOSE_MOVE`). A larger move refuses. With an original that holds 2 s of its audio twice,
the new file would play the rest 2 s late. An earlier move refuses, so a gap of the original never closes. A later move that is not one
such chain refuses too. The proof entry counts the frames that moved so in `times.contiguous`, with the worst move
and the first time. The reason is the picture. In mpv the original played the audio 51 to 74 ms early against the
picture of the release, 151 ms at worst, and mkvmerge's copy 0 to 18 ms early. That measurement covered moves up to
150 ms. VLC ignores the stored block times and showed no change. Of that release's 6,767 overlaps, 6,685 follow a
lace, and the plan plays 528,371 frames on. The 82 after a one-frame block keep their times. Any other move past the
slack refuses, and the refusal names the count of moved frames, the worst move, the first time and the original's
overlapping blocks. Up to 2.18.8 the proof passed a move after any overlap up to 0.2 s.

**Planned audio times.** mkvmerge 102 times some audio frames on its own. Where a frame's time is not later than the
time before it, as at an overlap of the blocks, it plays the audio on by any amount. An episode with a 2475 ms overlap
to its end would play its last 24 s that much late. A lace with no DefaultDuration gives mkvmerge no time for its
other frames. For MP2 and MP3 it writes wrong ones (0, 26, 0, 26 ms), up to 144 ms off (`LACE_RETIMED`). It also times
Vorbis from ffmpeg again, up to 2 ms off (`RETIMED`). So a remux of a Matroska source plans the frame times of some
audio tracks (`remux.planned_times()`). A track gets a plan when it holds an overlapping block, a bitstream time of
MP2 or MP3, or a codec in `RETIMED`. A plan closes only an overlap after a lace, see "Overlapping blocks", and keeps
every other overlap as the original stores it. mkvmerge gets the plan in a
v4 timestamps file (`--timestamps`). It keeps each time in the order of the file's lines and rounds it to its
TimestampScale. The subtitle remux, the header repair and the conversion of a Matroska source, such as a WebM, write
the plan. mkvmerge keeps the stored and default times of every other track. AAC, AC-3, E-AC-3, DTS with a
DefaultDuration, FLAC and Opus from ffmpeg measured the same. So did the lace frames of AC-3, E-AC-3, DTS and Opus
with no DefaultDuration, which hold bitstream times. It times TrueHD up to 1 ms off, within the slack, and both times
are right, see "The slack" under "Frame times".

Each frame keeps its stored, default or bitstream time. Past a block that starts before the end of the lace before
it, the frames play on. Each one then starts one frame after the one before it, until a later gap of the original
brings its own time back. A run that no gap ends keeps the stored time of each of its frames. That is a run to the end
of the track, and one that reaches the cap below. The joined parts above give such a run. The move may reach `CLOSE_MOVE` less half a millisecond. `CLOSE_MOVE` is also the move the proof accepts (0.2
s). The new file rounds each planned time to the millisecond, so a move closer to 0.2 s could round past it. A
larger overlap keeps every stored time, so the new file holds it as the original does. A move within the rounding of
the two files (`mkv_slack()` plus half a millisecond) stays. The proof counts it as a frame that kept its time, and
the frames after it would then follow no overlap and refuse. A frame that shares its time with the frame before it
plays on as any frame does. So a plan steps back in time only where it keeps an overlap. That is an overlap after a
one-frame block, or one whose run no gap ends. With `CLOSE_MOVE` at 0, every frame keeps its time.

`remux.planned_args()` checks each plan with the proof's time check (`times_fault()`). The check rounds each planned
time half up to the tick of the new file, as mkvmerge rounds it. That tick is 1 ms where a video track stays. mkvmerge
gives a file of audio alone one sample of its highest audio rate, less 1 ns and cut to whole ns (`remux.mux_tick()`,
20,832 ns at 48 kHz, measured from 8 to 192 kHz and with two rates). It reads the rate of AAC, FLAC and Opus from the
frames, which can differ from the rate the file stores. So the remux of a file of audio alone gives mkvmerge the tick
of the stored rate (`--timestamp-scale`), and the check and the new file use one tick. Where the two rates agree, the
new file holds the same bytes as without the option. When the proof would refuse the plan, every frame of that
track keeps its time. A plan that keeps an overlap has a worst drop, how far a frame falls below the latest planned
time before it. The drop comes from the planned times rounded to the tick, as the new file stores them. When that drop fails the rule of "Where a packet sits" against the same drop in the stored times,
every frame keeps its time too. So no writer writes a plan that its own proof refuses. The header repair runs no
proof, and it writes no such plan either. The proof counts the frame length of a run in fractions of a microsecond, so a run of
any length passes. It refuses a plan that moves the earliest frame of the track. In an original finer than 1 ms, a
frame and the first time it counts from each round in the new file. The slack holds both, see "The slack" under
"Frame times". On a film of 10.9 GB the check took 1.4 s, and it kept the plan.

mkvmerge sets the DefaultDuration of a planned track from the steps of its timestamps file. A track whose blocks store
whole milliseconds gets 43 ms for frames of 42.667 ms. A lace of default times keeps its value. `--default-duration`
does not change that. So mkvpropedit gives each planned track whose DefaultDuration changed the original's again, or
removes it where the original holds none. It edits the temp file in place before the swap, and each writer's layout
check reads the file after it. `plan_fault()` then reads each planned time back from the new file, within half a tick
of its TimestampScale, and a track that differs fails the remux. The header repair runs no proof, so this read is its
check of the audio times. A file of audio alone gets a finer TimestampScale (20,832 ns for a 48 kHz track), so a plan
holds whole nanoseconds. On a film of 10.9 GB the plan and its timestamps file of 10.5 MB took 2.1 s, mkvmerge -J 0.1
s and mkvpropedit 0.15 s.

A track gets no plan when a frame's time is not known, as for the first lace of a Vorbis track with no
DefaultDuration. It gets none either when a frame holds no data, or when it is PCM, which mkvmerge cuts into other frames. A header repair that
laces audio again writes no plan, because a lace stores the time of its first frame only. It laces again only when no
audio track holds an overlapping block. Those tracks keep mkvmerge's timing, and the proof decides as before.

A measured film stores 6,767 DTS blocks that overlap the block before them. The plan plays 528,371 of its frames on,
up to 150.3 ms, with the frames that share a time with the frame before them. mkvmerge's own copy played 400,827 on. On that DTS track mkvmerge returns to the stored time at the
next block that starts after the block before it ends. It does so by any amount, so the rest of the overlap stays
there. On unlaced AAC it plays on until the stored time comes back, as the plan does.

**Other containers.** An MP4 and a TS source read their stored times too, see "Time reads".

## Time reads

Every time that a check or a write takes comes from the file as it stores it, or as its spec defines it. ffmpeg
and ffprobe estimate the time of a frame their parser splits off, and their muxers move a time that goes back. Each
container has one reader. `proof.stored_places()` takes the one of the file's format, and so does the packet read of
the audio checks, `checks.stored_frames()`.

| Container | Reader | Times |
|---|---|---|
| Matroska, WebM | `matroska.frames()` | each block's time, the frames of a lace by RFC 9559, see "Frame times" |
| MP4, MOV | `mp4.samples()` | stts, ctts and the edit list (ISO/IEC 14496-12 8.6.6), then ffmpeg's start rule |
| MPEG-TS, M2TS | `ts.frames()` | each PES PTS (ISO/IEC 13818-1), the frames of an audio PES by their codec lengths, TrueHD and LATM too |
| AVI | ffmpeg | the container's own definition, frame k of the index at k / rate, audio by its bytes or chunks |
| ASF, MPEG-PS, others | ffmpeg | an estimate, held to `SYNC` and `TIME_SLACK` |

**MP4.** The reader reads the top-level box headers and the moov box. Each sample gets its byte, its size and its
time. A sample that no edit presents is cut, and it keeps its time on the edit's line, as ffmpeg and mkvmerge time
an AAC priming frame. An audio track whose stts is one entry of delta 1, as PCM stores it, gets one entry a chunk, as
ffmpeg reads it. One entry a sample took 35 s and 577 MB for a 3-minute track. Movie fragments, more than one media
edit and a media rate other than 1 give no times, and the proof refuses with that reason. ffprobe names each MP4 stream's track_ID in its id, and the byte of each packet pairs
it with its sample, so ffmpeg's own edit-list choices never move the pairing. The video then takes ffmpeg's start
rule, see "Conversion".

**TS.** The reader walks the 188 or 192-byte packets. It reads the PAT, the PMT, then the PES headers of each PID. A video PES
holds one frame at its PTS. An audio PES splits into frames by the byte length each frame header gives (ADTS AAC,
AC-3, E-AC-3, MPEG audio, DTS core). A TrueHD access unit gives its length in its first 2 bytes. A unit with a
major sync names the rate code, which gives 40 samples at 48 or 44.1 kHz, doubled by its low bits. A LOAS frame of
LATM AAC starts with the sync word 0x2B7 and a 13-bit length. A frame that holds its StreamMuxConfig names its
subframes of 1024 or 960 samples and its rate. A Blu-ray TrueHD PID holds its AC-3 core in PES of extension 0x76, and
ffmpeg reads them as a stream of their own. So the reader gives the core frames of its own, and the TrueHD frames
leave those PES out. A PID whose stream type names no codec takes it from its first PES, as ffmpeg probes it. That PES must start
with a TrueHD major sync or a LOAS sync word. Frame 0 of a PES with a PTS is stored, and each frame after it is bitstream at
the PTS plus the lengths before it. Bytes that start no frame, as a broadcast splice leaves them, belong to the frame
before, as ffmpeg's parsers keep them. ffmpeg's LATM parser puts them in the frame after instead. So a LATM frame
after such bytes takes the PTS of the PES where they start. A cut LATM stream that starts at a false sync word showed
it, up to 213 ms off ffprobe's times. The 33-bit PTS unwraps from the file's first PTS. The k-th frame of a PID pairs
with ffmpeg's k-th packet of the stream whose id is that PID. ffmpeg times a TrueHD unit at 44.1 kHz 81 ticks after
the one before, where the definition gives 81.63. So its times run up to 0.7 ms early within a PES. A lost sync byte, a
continuity count that skips or a PES that does not read stops the read. The proof then refuses with that reason, and
the packet read of the audio checks gives no verdict. An 18.3 GB recording read in 65 s cold, where ffprobe's
packet read took 89 s.

**PCM.** PCM has no frames. Both files cut it again into packets of 4096 samples (`PROOF_BSF`), and ffmpeg's
`pcm_rechunk` times each packet from the stored time of its block and its sample offset. The proof keeps those
times for PCM in every container.

**AVI.** An AVI stores no times. Frame k of the video index plays at k times the stream's scale over its rate, after
its start (dwStart), and ffmpeg reads exactly that. So AMG reads AVI times through ffmpeg, and `avi_sync()` gives
mkvmerge the start back. ffmpeg counts audio from its bytes and mkvmerge from its samples, and the two drift over a
film, so the proof leaves AVI audio out of the time check and keeps its start and its data.

**The slack.** A Matroska copy of an MP4 or TS file rounds each time to its tick, and mkvmerge moves the file by its
earliest time, which is not on that tick. A time and the time it counts from each round, so a move reaches one tick
of the new file, `mkv_slack(exact=True)`. Between two Matroska files, a time counts from the video's start or the
stream's first time, which a new file with a coarser TimestampScale than the original rounds too. So `mkv_slack()`
takes half the difference of the two scales more, and the time check and the start check share that bound. A track
that moves 2 ms refuses.

**The guard.** `ff.py` makes every ffmpeg, ffprobe and PyAV call. Each call names its use, and `ff.READS` names
the times that use may read, with the reason, such as the md5s of the proof's packet data, the decoded frames of a
picture window, the AVI read above, and a container duration that gates a check by length. A new read needs its
reason in review. `ff.py` allows only what it knows. Anything else raises `TimeRead`, and the check errors.

- **The argv.** `ff.check()` parses each call. Each option, log level, ffprobe section and field, output format,
  filter, bitstream filter, muxer and protocol must be on an allow list of `ff.py`. Each one that gives a time must be
  named by the use. These are an ffprobe time field, a muxer other than null, data and s16le, a filter that logs a
  time, setts, `-progress`, `-debug_ts`, `-report` or `FFREPORT`. A filter and a bitstream filter take only the
  arguments AMG gives them, because `print()` in an expression of asetpts or setts logs a time for each frame. `-ss`,
  `-t` and a `-read_intervals` time need `seek`, because ffmpeg's own frame times place that cut. Each ffmpeg output
  names its muxer with `-f`. A lavfi input is a filter graph. Only `ionice` and `nice` may come before ffmpeg or
  ffprobe, and the argv is a list with no shell. A named ffprobe field that the call does not ask for raises too.
- **The output.** Text that needs a filter must come back as captured text, or to an `ff.Log` for a `popen()`.
  ffprobe JSON keeps the known keys of each section, nested ones too, so the DURATION tag goes, and `ff.loads()`
  gives mappings that raise on any other key, with `[]`, `.get()` or `in`. At level info ffmpeg prints the input dump
  (Duration, start, Chapter) and its stats line (`time=`). So a call at level info names each line's level with
  `level+info`, and its stderr keeps only the lines of named channels and the lines below info. A use that names
  `dump` keeps the stream lines. showinfo, silencedetect, volumedetect, `-progress` and framemd5 text keep only their
  named time fields. `ff.run()` filters a framemd5 file when the call ends, and finds a relative path under `cwd=`.
  The large reads (the packet reads of the proof and of the audio checks) name every field they ask for, so no filter
  runs on them.
- **PyAV.** `ff.pyav()` wraps every object PyAV gives, the classes and the submodules too. Each kind of object allows
  its known attributes and the times the use names. So metadata, chapters, a subtitle set and a frame's sample count
  raise. A filter graph, a resampler and an audio frame allow no attribute, because no use needs one. `repr()` shows
  no time. A source scan checks that no module but `ff.py` reads the object behind a proxy (`_o`, `_unwrap()`).
- **The launch guard.** `ff.py` installs an audit hook (`sys.addaudithook`) once per process, and the package's
  `__init__.py` imports it. So the job processes, the scripts of the language-ID venv and any pool worker have it.
  Package code may start ffmpeg and ffprobe only through `ff.run()` and `ff.popen()`. It may start a Python only to
  run a script of the package. Else it may start only mkvmerge, mkvextract, mkvpropedit and logrotate (`ff.OTHERS`).
  The hook reads the program at `argv[0]` or after `ionice` and `nice`, and the program of `executable=`. Any other
  start raises, so a launcher such as `env`, `sh` or `timeout` raises. The hook sees `subprocess`, `os.system`,
  `os.exec*`, `os.posix_spawn` and, on Python 3.14, the start of a multiprocessing child. A start whose callers are
  all in the standard library counts as package code, as a thread or a pool that runs `subprocess.run()` itself. A
  lookup of a C function through `ctypes` by package code raises, and so does an import of PyAV by a module other
  than `ff.py`. Code outside the package, as a test or faster_whisper, passes. The source scan refuses an import of
  `ctypes`, `_posixsubprocess` or `multiprocessing` in the package, and the name `ProcessPoolExecutor`, because
  Python 3.13 raises no event for a multiprocessing child.

Each audio decode whose times meet the cues, the onsets or the video goes through `ff.clock()`. These are
`lid.extract()` for the word windows and the burn-in windows, `lid.speech()` for the speech spans, and
`subtitles.onsets()`. Its `aformat` first mixes the track to mono 16 kHz s16 in one swresample, with the matrix of
`-ac 1`. Then its `aresample` puts each frame at its time from the start of the read, the file's start or the `-ss`
time, a named read. It fills a late start and a gap of 0.1 seconds or more with silence. So the samples count on the
file's clock, and a fill costs 32 kB a second. A fill before the mix cost the channel count times the input rate in
floats. A made-up 5.1 track with a gap of 30 minutes took 2.3 GB, and a damaged film with gaps of up to 31 minutes
took 1965 MB. A float `aformat` mixes 5.1 with another matrix than `-ac 1`, so the mix is s16. A healthy track then decodes
sample for sample as `-ac 1 -ar 16000` alone. volumedetect's `n_samples` counts decoded samples, and a use names it as a count.
`checks.sample()` and `checks.full_decode()` read that count with no fill, because a fill would hide the loss they
find. Error and warning lines, as "Non-monotonic DTS", are no channel.

Each refusal writes one line to `STATE_DIR/ff-faults.jsonl`, because many callers catch every exception. `config.py`
names the file at load, so each entry has it: the hook, the worker, the subtitle hunter and the listener. Every child
inherits the name. `logs.decision()` reads the refusals of its run, in its process and its children, and makes the
decision line an error that names the use and the field. The nightly audit counts it as an error. A dry run, a scan
and a lid.py child of the subtitle hunter write a decision line that the audit leaves out, or none. So the audit's
summary line also counts every fault line of its window as `refused`. logrotate rotates the file with the decision
log.

`tests/conftest.py` fails a test when a call, a read or the audit hook raised `TimeRead`, in the test's process or a
child. The source scan of `tests/test_time_reads.py` is the second line, for the paths no test runs. Each time word
or attribute in a function or class must be a name of a use that it calls, or reads for as a reader that
`ff.READS` names, as `decide.FRAME` reads the window's showinfo text. Each name of an entry must show in the code of
its use.

## Header repair

A Matroska header can be wrong while every frame is whole. A lossless mkvmerge remux writes the
Segment duration, the Cues and the Segment size again from the streams. Real damage is never
repaired and keeps its re-grab. `HEADER_REPAIR=false` turns the repair off, and the check still
reports.

`header_probe()` reads the file start, the Cues and the elements of the RFC 9559 check, see "RFC 9559 check". It
reads the last Clusters too when the Cues are missing or the Segment size runs past the file end. It also reads them
when the last cue is far off the header duration, when any byte follows the Segment end, or when the RFC 9559 check
finds a fault. When a subtitle may run past the end, the walk reads every subtitle block, and a block with no
duration ends at the next block of its track (`subtitles.subtitle_ends()`, RFC 9559). A remux fixes missing Cues,
a wrong duration, a Segment size left by the hook's failed edit, a broken layout, a tail, enclosed zeros and each
fault of the RFC 9559 check. A SubRip line past the end gets the trim or the removal. Another subtitle codec past the
end has no fix and posts one amber embed. Missing Cues, a wrong duration, the Segment size of a failed edit, a
subtitle past the end and enclosed zeros are repaired before the checks, because the checks cannot read through them
(`hp["first"]`). mkvmerge reads no packet past enclosed zeros in a subtitle remux. A layout issue, a tail and an RFC
9559 fault wait for the write stage, which carries the flag edits in the same remux, see "Write stage". With no flag
edit, the write stage runs the header repair alone.

**The layout.** RFC 9559 wants Info and Tracks before the first Cluster, or listed in a SeekHead before it (6.1). A
second SeekHead may list Clusters only (6.3). `checks.layout_fault()` walks the elements before the first Cluster by
their headers and reads each second SeekHead that a front one lists. A file that breaks either rule has a header
issue, so `--check` and an import send it to the remux. The ffmpeg subtitle remux broke both. ffmpeg leaves no room
after Tracks, and mkvpropedit then put back properties that made Tracks grow. mkvpropedit moved Tracks past the
Clusters, copied the SeekHead to the end and left a front SeekHead that lists only that copy and the Tags. MediaInfo
showed no track in such a file. Tracks at the end that the front SeekHead lists are valid, as Lavf writes them and as
a flag edit of a file with zero padding leaves them (see "Zero padding"). An old mkvmerge's second SeekHead of
Clusters is valid too. `checks.seeks()` still follows a second SeekHead to the Cues, so such a file reads as before.
A flag edit checks the layout of its result, and a layout it broke gets the repair in the same run, see "Write stage".
mkvtoolnix keeps this mkvpropedit behaviour on purpose
([issue 6322](https://codeberg.org/mbunkus/mkvtoolnix/issues/6322), 2026-10-19), so the repair stays.

**Enclosed zeros.** An older flag edit of a file with zeros past the Segment end left them inside the Segment, see
"Zero padding". `header_probe()` finds the run by the rule of `checks.zero_pad()` and names its start in `enclosed`.
That is a header issue. mkvmerge reads the file up to the run, warns that it resyncs, and finds no element after it.
The repair accepts exactly those lines for each input of the file: the byte position, at or after the run, the last
timestamp it read, at the end of the streams, and the failed resync. Any other line fails it. The size check counts the
original up to the run.

The remux runs only when the video check ran all its stages and found no certain fault and no doubt.
The video and the audio must also end at most 60 seconds apart. It runs like a conversion, but it
keeps the name and the original, and it keeps the track order and every UID. The new file must pass
these checks:

- mkvmerge exits 0 with no warning.
- The same tracks, in the same order, with the same properties and UIDs. A property changes only as a row of
  "mkvmerge rewrites" says.
- The streams end within 1 second of where the original's streams end, usable Cues, and no issue left. Both ends
  come from the last blocks of each file, read the same way. mkvmerge writes the Segment duration from the first
  block, so a file whose blocks start at 2.5 s has a header 2.5 s shorter than its stream end.
- Every video frame the original stores, each at its time, up to 3 frames at the end (`FRAME_SLACK`). mkvmerge can
  drop a zero-filled Cluster with no warning, and this catches it. The walk of the original's blocks counts its frames,
  see "Frame times", so a Cluster that a demuxer skips still counts, and a frame that an encoder left out is no loss.
  Each frame keeps its own time, as mkvmerge keeps the times of a Matroska file, so a frame lost at the start
  refuses too. A new file that lacks frames refuses with the time of the first frame it lacks, when the frames after
  it sit that many places on. Else it names the move. When the walk of the original stops before its end, the count
  is not known, and the repair refuses with the walk's reason. A walk that read every Cluster whole still counts: a
  Segment size past the end of the file, or a level-1 element after the last Cluster whose size does not read, with no
  Cluster id after it in the file. A failed edit leaves such a file, and the repair exists to fix it. A second file
  that cat joined after it holds a Cluster id, so the count is not known.
- As many streams of each type as ffprobe reads in the original, less the tracks the repair removes. mkvmerge 102
  -J lists no Opus, S_KATE, S_DVBSUB or HDMV TextST track with no CodecPrivate, and its remux drops that track with
  exit 0. The tracks check compares two mkvmerge probes, so it misses that loss. The ffprobe read of the original
  comes with the walk for the frame count, so a new file with no main video gets no such check.
- A size within 3 percent of the original Segment, and 3 clean decoded windows.
- Each stream's lag passes the rule of "Where a packet sits" against its stream in the original. The walk of the
  new file gives the positions, and the walk of the original for the frame count gives its lags. The k-th stream of
  a type pairs with the k-th track of that type in the original, less the tracks the repair removes. A new file with
  no main video walks no original, and 1 second alone bounds it. `header_repair.lag` keeps both lags per stream.

**A variable frame rate.** Some files hold frames of more than one length in one video track. They still store one
DefaultDuration. HandBrake mixes film parts at 23.976 fps and video parts at 29.97 fps from a telecined source. A
DVD's MPEG-2 repeats fields, so some frames last 1.5 frames. An encoder that drops repeated frames leaves steps of 2
or more frames all through the track. Such a track holds fewer frames than its end over its default duration. So the
frame count above refuses every repair of it.

The read that gives the lags also gives the time of each frame of the new file. A step from one frame to the next is
off the rate when it differs from the median step, the track's usual frame length. It may differ by a tenth of that
length (`VFR_STEP`), or by the timestamp scale when that is larger. Mixed rates differ by 15 percent or more. An
encoder's jitter of 2.3 ms on a 41.7 ms frame fits. The DefaultDuration plays no part, because some files store the
length of one field there. With 1 percent of the steps off (`VFR_SHARE`) or more, the rate is variable. A lost
Cluster makes one long step. Each frame an encoder left out makes one step of two frames. So a constant rate that lost
frames in a few places stays far under 1 percent, and it keeps the frame count. The import's alert counts a step of
whole frames as on the rate, see "Corrupt video". The repair counts it as off, so such a track still repairs.

The frame count holds every stored frame time of the original, so a variable rate needs no rule of its own. The
record keeps the share of steps off the rate and the verdict in `frame_rate`, and the original's frame count in
`original_frames`. No check takes `frame_rate`. A HandBrake
0.9.4 episode of 44437 frames has 36 percent of its steps off its usual 23.976 fps frame length. Its repair now
passes. A constant rate whose encoder left frames out in a few places still fails the count. An old WEBRip with 12
frames left out at 5 cuts does, because a frame left out and a frame lost look the same.

**The subtitle tracks come from a second input.** A player reads the file in order and never shows a
packet stored minutes after the packets of its time. The ffmpeg subtitle remux of 1.4.0 to 2.17.0 left
rewritten tracks up to an hour late in the file. mkvmerge reads one input in file order. It holds back
about 20 MB of packets while it waits for a late one, and past that it writes the late packet where it
finds it. So the remux names the file twice. The first input gives every track but the subtitles, with
the chapters, the tags and the attachments. The second gives only the subtitle tracks. mkvmerge reads
each input on its own and writes the packets of all tracks by their times. It keeps the packet order of each track,
so a packet stored after a later packet of its own track stays behind it, see "Where a packet sits". The second
input reads the whole file again.
A file with no subtitle track has one input. mkvmerge compresses PGS and VobSub tracks with zlib by
default, and some hardware players read none. So a track that the source holds uncompressed stays
uncompressed, as in the subtitle remux, see `remux.compression_args()`.

**Each audio frame in its own block.** The remux passes `--disable-lacing`, as a conversion does.
mkvmerge would lace the audio of an unlaced source. A player that seeks to 0 skips each block before the
first video keyframe, and a lace there holds up to 170 ms of audio. An audio track that mkvmerge would time on its own
gets its planned times, see "Planned audio times", and `plan_fault()` reads each one back. A plan that would put
its audio further behind the other streams than the rule of "Where a packet sits" allows keeps the stored times.

**Audio that holds ffmpeg's estimates is laced again.** ffmpeg copies a lace frame by frame. It puts each
frame in its own block, at a time it estimated from floored durations, up to 2 ms early for AAC. mkvmerge
copies each stored time, so `--disable-lacing` would keep the estimates. Before the remux, `lace_guess()`
takes the stored time of each block among the first 500 frames of each audio track. The head walk gives them, see
"Frame times". The frame length gives an even frame clock. For AAC, AC-3, E-AC-3, DTS and MPEG audio it
is the length the codec defines. mkvmerge 102 writes that length in the DefaultDuration of a relace. So a DefaultDuration that
another tool set, as 20 ms for AAC, does not hide the estimates. Another codec takes the DefaultDuration, else the
length its codec defines. A track with no defined frame length, as Vorbis, marks nothing. Two signs together mark the
estimates (`LACE_GUESS`, `LACE_STEPS`).

- The block times spread more than 1.5 ms around the clock. True times on the millisecond grid spread
  0.98 ms at most in the real files measured, and ffmpeg's estimates spread 1.67 ms (DTS) to 2.4 ms (AAC).
  A spread over 10 ms is a gap or mixed frame lengths, as Vorbis has, and marks nothing.
- At least 3 percent of the steps from block to block fall outside the frame length times the frames between the
  blocks, rounded down or up to the TimestampScale. ffmpeg's estimates catch up once per lace, 4.2 to 10 percent
  of the steps.
  True times with one jump of a few milliseconds, or a slow wander of about 1 ms, spread as far but have 0.2
  percent at most.

When any audio track holds estimates, the remux runs without `--disable-lacing`, and with no planned times. mkvmerge
102 laces all tracks or none, and a lace stores the time of its first frame only. mkvmerge groups the frames into
laces as it did before ffmpeg's copy. So each lace starts at a frame whose stored time is true, within half a
millisecond of the sample clock. The record names those streams in `relaced`, and the dry run's result names them too.
When an audio track starts before the first video keyframe, the estimates stay. A seek to 0 then never loses more audio
than the file does now, and `relace_skipped` says why. When an audio track holds a block that starts before the block
before it ends (`remux.overlapped()`), the estimates stay too, and `relace_skipped` says so. A relace gives mkvmerge no
plan, so mkvmerge would play that overlap on. A made-up copy with an overlap of 30 ms after a one-frame block moved 335
of 939 frames up to 31 ms. With `--disable-lacing` its plan keeps every stored time, see "Planned audio times".
Each video block stores its keyframe flag. A SimpleBlock holds it in its flags, and a BlockGroup with no
ReferenceBlock is a keyframe. Each start counts the track's CodecDelay, as a player plays each block at its time less
that delay (RFC 9559).

**The trim and the removal.** The trim runs when some track ends over 60 seconds or 2 percent past
the video and the audio. It cuts the late SubRip lines at the end. A track with at least
`REMOVE_MIN` (2) lines, and `REMOVE_SHARE` (10 percent) of its lines, after the real end is timed
for another cut. The hook removes that track when it also ends past `REMOVE_END` (1.2) of the listed
runtime, because a wrong track is worse than none. It trims the other late tracks. A PGS or VobSub track would need a new encode, so it
stays. A trimmed track goes into the remux as a Matroska file that holds its UID, see `remux.uid_files()`, so it keeps
its UID as every other track does.

**A cut file keeps its subtitles.** A cut file's subtitles run to the full length and would meet the
removal rule. So nothing changes when the video and the audio end under `CUT_END` (0.95) of the
listed runtime, or when no runtime is listed. One amber "File may be cut short" embed goes out instead. It
also covers a track from another episode, so check the last frames by hand.

**The tail.** A download can write a shorter copy over an old file and leave the old bytes past the
Segment end, and some players decode them. Some muxers end a file with a few zero bytes past the Segment end, which
the check calls padding. Bytes past the Segment end are a header issue at any count (owner 2026-10-20). The remux cuts
them when the video and the audio reach the header duration. Over 1 MiB (`TAIL_MIN`), a Segment that is itself cut
stays, with a video doubt. Up to `TAIL_MIN`, a Segment whose streams stop before its stated length blocks the repair
(`edit_blocked`), and the repair is skipped. Bytes that start with the EBML id `1A45DFA3` are a second file joined with
`cat`, and they stay.

A failure keeps the original and posts one amber embed, and the flag edit still runs, see "Write stage". After a
repair before the checks, they read the new file. After a repair in the write stage, the run plans the new file again.
The app rescans the item once. A backfill dry run reports
`would_repair_header`, and `--apply` repairs. The line of the dry run starts with `dry run`, as for a flag edit. A
doubt or a fault that blocks the repair gives `header_not_repaired`. Its result names the block, then the issue, as
`not repaired: the video ends at 1:54:58 and the audio at 1:50:12. The issue: the header says 13:23:33, but the streams
end at 1:54:58`. The line of a backfill shows the first 300 characters of a header result.

## mkvmerge rewrites

mkvmerge 102 writes a few track properties again in a remux. `remux.REWRITES` holds one row for each change that AMG
accepts. A row names the codecs it takes, the mkvmerge -J properties it may change, the change, and one function that
accepts that change and nothing else. `remux.tracks_fault()`, `remux.props_fault()` and `remux.header_kept()` read the
table, so the header repair and the subtitle remux take the same rows. Any other change of a kept property refuses the
remux. The rows:

- BCP 47 tag added. A track with a legacy tag and no BCP 47 tag gets the tag that mkvmerge's language table gives it.
- Legacy tag added. A track with only a BCP 47 tag reads as eng, and mkvmerge writes the legacy tag of its language.
- A legacy 639-3 code. mkvmerge writes a code its 639-2 table lacks, such as yue, as the BCP 47 tag, and the closest
  639-2 code, such as chi, as the legacy tag. That code must be und or name the same language, see
  `decide.lang_key()`, so zsm to may refuses. The code any keeps its refusal. HandBrake writes any for Any, and BCP 47
  reads it as the Anyin language.
- AVC header. Bytes 1 to 3, the profile, the compatibility flags and the level, take those of the first SPS.
  mkvmerge 4.9.1 wrote them as 0.
- VC-1 header. biSize of a BITMAPINFOHEADER of FourCC WVC1 becomes the header's length, and biXPelsPerMeter and
  biYPelsPerMeter become 1 where they are 0. ffmpeg writes a byte of padding past biSize and both as 0.
- Vorbis comments. mkvmerge empties the comment list and keeps the vendor. The identification and setup headers stay
  byte for byte. It makes a METADATA_BLOCK_PICTURE comment an attachment, so a header that holds one refuses.
- VP9 header. mkvmerge adds Codec Feature Metadata where the track has no header. Each record holds 3 bytes, an ID
  from 1 to 4, a length of 1 and the value. The IDs come in ascending order, and IDs 1 and 3 always come.
- ASS header. The text round trip of the subtitle remux writes the header's lines in order, less a NUL at its end and
  the blank lines at its end, each followed by CRLF, then one blank line. A header with no `[Events]` section, in any
  case, gets a blank line, `[Events]` and the Format line of its codec before that blank line. Only a track that the
  round trip wrote takes the row.

Every AMG remux keeps every track UID, and `remux.tracks_fault()` refuses any UID change.
A track with no UID, or a UID that another track holds too, takes a new UID from mkvmerge, so its remux refuses.
MakeMKV track UIDs keep their refusal. mkvmerge gives every track of a file whose writing app holds makemkv in its
first word a new random UID. A selector names a track by its UID. A queued plan line, an audited follow-up and the
undo line of an earlier edit name the tracks of the original, so a new UID breaks them. In the census of 2026-10-24,
41 files that MakeMKV wrote took new UIDs, and none of them had a header issue to repair.

To add a row, take a census with the new mkvmerge. Remux a few files of each kind in the library (codec, muxing app
and writing app) with the header repair's own command, and compare every track property of each pair. A change that
no row accepts refuses the remux. Read the source of mkvmerge for that change, then add one row and its function. Give
the row a test of the change it accepts and one test of each near change, in `tests/test_rewrites.py`. Take the census
again at each mkvmerge bump.

## RFC 9559 check

`header_probe()` holds the elements outside the Clusters to the Matroska schema, see `matroska.check()`. It reads the
EBML header, each level-1 element before the first Cluster, and each one a SeekHead lists. It never reads a Cluster.
It reads a master whole up to 1 MiB (`SPAN`), else by its element headers, so the fonts of an Attachments element cost a
few headers. It uses the Cues that the header check read already. `matroska.TABLE` holds the 190 elements of RFC 9559
section 5.1 outside the Clusters, with their place, type, occurrences, minver, range and length.

| Kind | Fault | Who notices |
| --- | --- | --- |
| `padding` | 1 byte to `TAIL_MIN` past the Segment end | no reader |
| `tail` | more than `TAIL_MIN` past the Segment end | players that decode the old bytes |
| `enclosed` | zeros a flag edit left after the Cues, see "Enclosed zeros" | mkvmerge and ffmpeg |
| `layout` | Info or Tracks out of reach, a second SeekHead of more than Clusters, see "The layout" | MediaInfo |
| `version` | a DocTypeVersion below the minver of an element the file uses (RFC 9559 7) | mkvalidator |
| `range` | a number out of its range, as a SamplingFrequency of 0 or a DisplayWidth of 0, or a size its type does not allow | mkvalidator |
| `schema` | an element out of its place, as MuxingApp written as 0x80 in Info, a mandatory element missing, too many of one, a CRC-32 that does not match, a SeekHead entry where no such element starts | mkvalidator |
| `framing` | an element in a header master that does not read, has an unknown size, or runs past its parent | ffmpeg and ebml_validator |
| `tail_errors` | bytes past the Segment end after a last element that holds level-1 ids, as a libmkv SeekHead of Clusters | ffmpeg's read pass |

`hp["rfc"]` maps each kind to its faults, and each fault is a header issue. Most faults name no byte, so a remux that
writes one again names it the same way. A framing fault and a SeekHead fault name a byte. An element RFC 9559 does not
define is a note in `hp["notes"]`. A reader skips it (RFC 9559 7). mkvmerge 102 writes two of them, EditionFlagHidden
and ChapterFlagEnabled, when it makes chapters from a simple chapter file (`CHAPTER01=...`). A file with XML chapters
holds neither. The decision line holds the kinds in `rfc`, and the header stage of the
video check holds the faults, the notes, `read` and `took`.

**tail_errors.** After a read error, ffmpeg looks for a level-1 id from the byte after the start of the last level-1
element it read (`matroska_resync()`). A byte past the Segment end is such an error. When the last element is a SeekHead,
ffmpeg finds a Cluster id in each of its entries and prints an error at each one. A libmkv file with one zero byte past
its Segment end printed 651 lines, and none with that byte cut. A second SeekHead at the end that lists Cues gives
"Truncating packet" lines that name no place. A library film printed 5 of them, and none with its byte cut. The
whole-file check of 2.18.4 posted "Video may be broken" for both. The check finds the case without the read pass, and
the whole-file check counts those lines as the tail fault, see "Whole-file check".

**The repair.** Each kind is an issue, so an import, `--check --apply` and a backfill with `--apply` repair it when they
find it. With a flag edit, the edit rides the repair. With none, the header repair runs alone. A dry run writes nothing.
An alert goes out only when the repair fails, as for any header repair. mkvmerge 102 writes each element again from
what it reads, and the made-up plant of each kind ends with no fault after the repair. `remux.header_checks()` refuses a
new file that still has an issue. A fault of the schema kinds that the original held too fails no repair, because
mkvmerge wrote it again. `header_repair.rfc_left` then names it, see `checks.rfc_again()`.

**The library pass.** About 7.8 percent of a library holds padding of 1 to about 1,000 bytes, and a repair of each would
rewrite terabytes. So the first pass over a library repairs the real faults only:

1. `--backfill APP --check-video` shows which files have a header issue. It lists them for a person in
   `STATE_DIR/video-scan-APP.txt`, and writes that list again at the end of each run. A file whose only issue is
   padding gets its decision line and no list line. No later step reads the list.
2. `--backfill APP --plan-out /config/plans/APP-header.jsonl` is a dry run. It reads each file and writes its plans to
   that new plan file. It plans every `.mkv` file the app lists, unless `--ids` or `--paths` names some. It changes no
   media file. A plan line whose header repair names padding alone holds `"padding_only": true`.
3. `--backfill APP --apply --plan-from /config/plans/APP-header.jsonl --only-header` repairs the files of that plan with
   a header repair, except those with `padding_only`. A file it takes is repaired whole, padding too.
4. `--audit APP --since START` counts the files of each kind, by the latest line of each file in the window.

The plan file must be a new file outside `STATE_DIR`, because step 2 empties it first and AMG owns the files there.

**Cost.** `header_probe()` of 229 library files over NFS, read cold under a load of about 60 (2026-10-20). A file with
no issue read a median of 104 KB and at most 988 KB, of which the RFC 9559 check added a median of 1.2 KB and at most
594 KB over 2.18.4. A file with an issue reads the last 16 MiB for the stream ends, as before. The probe took a median
of 0.16 s, 0.86 s at the 90th percentile and 2.7 s at most. A walk of every Cluster header read 43 to 689 MB per file.
The test `test_a_clean_file_reads_no_cluster` holds the bound, and the decision line holds `read` and `took`.

## Cover track

A *cover track* is a video track of a still-image codec that holds the poster of the film, see "Corrupt video". Some
players show it in place of the film. An *attachment* is a file stored inside the MKV beside the tracks. The Matroska
convention names the cover art attachment `cover.jpg`. So AMG moves the image of a cover track into such an
attachment, in the one remux of the subtitle step (`remux.resub()`). It runs where that remux may change the file:
an import and `--check` at `SUBTITLES` fix or deep, the deep analysis, a recheck, and `--sub-check` and `--sub-time`
with `--apply`. It runs with `KEEP_ORIGINALS_DAYS=0` too, because it loses no byte. A dry run says what `--apply`
would do.

`remux.cover_tracks()` finds the tracks. Only a file with two video tracks or more is read. A track counts when its
codec is a still image, it is not the film, and every packet of it holds the same bytes. mkvmerge's frame count of 1
for this file needs no read. Another track is read once with `-c copy`, and frames that differ make it a real video
track. The attachment gets the name `cover.jpg`, `cover.png`, `cover.bmp` or `cover.gif` by the codec, with its MIME
type.

The file keeps one copy of each cover picture. A track whose picture an image attachment of any name holds byte for
byte goes, and the attachment stays as it is. Tracks of one picture that no attachment holds go into one new
attachment, `cover.jpg`, or `cover2.jpg` and so on when an attachment of any type has that file name, or an image
attachment has its stem, as `cover.png` does. A font named `cover.ttf` takes no name. A track whose picture nothing
else holds becomes `cover.jpg`, unless the file has an image attachment named `cover`, or the same remux adds one
first. The md5 of each picture decides. A 1-frame track's picture is read, with `-frames:v 1`, only when there is
something to compare it with. Existing attachments always stay, even two of the same bytes, as their names can carry
a role, as `cover_land.jpg` does. The track stays, and `covers` in the decision line says why, when:

- its frames differ, or they were not all read in the time left,
- the file has an image attachment named `cover` already, and no attachment or other track holds its picture.

A file whose video tracks are all still images, such as an audio file with a poster, has no film, and nothing moves.
A job that runs again with the file lock exclusive reads the file no second time, as `remux.COVER_READS` keeps the
reads.

The film is the first video track that is no still image, see `decide.main_video()`. A still image is MJPEG, PNG,
BMP or GIF, see `decide.still()`. mkvmerge names ffmpeg's GIF a QuickTime track with the FourCC `gif `. Every check
that reads the video takes the film: the windows, the burn-in samples, the packet read of the audio check, the last
packet and the DURATION tag of the video end, the frame count of a header repair, and the height of the length
check's bitrate ceiling. The burn-in samples seek by the film's own index, and so does the last packet read. A film in MJPEG,
as from an old camera, is a limit. With a short h264 track after it, the checks take the h264 track as the film. Its
frames differ, so the MJPEG track stays. An MJPEG film with no other video track is the film, see `decide.cover()`.

The remux leaves the track out of its first input (`-d !<id>`), with no `--compression` option. The ffmpeg read of the
original before the remux writes its first packet, and the md5 of each of its packets. A track that the check read
packet by packet already needs no second read of its md5s, which reads the whole file. Every md5 must be the same, and
the first packet must hash to it. A copy keeps a frame that is no keyframe, as a GIF frame is (`-copyinkf`). The same
mkvmerge call adds that packet as the attachment (`--attach-file`), with the track name as its description. A track that
goes as a copy adds nothing. mkvextract reads the attachment of each cover track back from the new file, and its bytes
must be the track's packet: the attachment this remux adds, or for a copy the one the file keeps or an earlier track
adds. It finds an attachment the file keeps by its UID, which mkvmerge keeps, and an added one by its name. Two
attachments may share a name, so `cover_tracks()` names an attachment by its id. `proof.prove()` matches every kept
track packet for packet, and the tracks keep their properties and UIDs. Any fault keeps the original. A done move adds
`cover_attached` to the reasons, and the app rescans the item. A remux that moved only covers logs "cover picture moved
to an attachment", or "cover picture track removed as a copy" when each track was a copy. With `DISCORD_POSTS=all` the
"File repaired" post says it. It names the attachment a removed copy matched, and the attachment of a picture that is
not the file's cover art, as `cover2.jpg`. Only an attachment named `cover` is the file's cover art.

A move that failed or could not run posts one amber "Cover picture not moved" alert, which ends "The original file
was not changed." When the same remux carried subtitle fixes, the alert that says why the rewrite failed takes one
more sentence for the cover instead, so the run posts one alert. Both name every cover track the remux was to move.
A run that re-grabs the file posts no cover alert.

## Kept originals

A header repair, a tail cut, a trim and a subtitle removal keep the file they replaced for `KEEP_ORIGINALS_DAYS` (7),
counted from the latest write, see "One original per file" below. 0 drops it at once. A conversion keeps nothing,
except a forced one and one that leaves out a subtitle track that does not match, see "Subtitle match". The original is
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
Each keep and the nightly audit remove the time folders older than the setting, see `stamp_time()`.

**One original per file.** A file has one kept original, its bytes before the hook's first write. That holds within a
run, where a header cut before the checks and the write stage can both write, and across runs, as a deep analysis
remux and a later library-pass header repair. Before 2.18.6 each write kept the file it replaced, so the second keep
held the hook's own output and a full extra copy for the setting's days.

- After each write that keeps, `vault.kept_write()` records in the state store, by path, the kept path and the
  inode, size and mtime of the file it wrote.
- `keep_original()` reuses that keep when the file at the path still has that inode, size and mtime, the kept file
  exists, and its time folder has more than an hour of the setting left. It keeps nothing new, returns the kept path,
  and sets `kept_how` to "kept at an earlier write". So every record and post of the write names the one original.
- A new grab, a hand edit, a restore from the kept file, a rename by the app and an expired keep each fail that check.
  The file then gets a new keep. A hash would read the whole file at each write. The ctime changes when a grab of
  `KEEP_REPLACED` hard-links the file, so it is left out. So a same-size write in place that sets the old mtime back
  reads as the hook's own output.
- The hook's own flag edit with mkvpropedit changes the mtime. After a successful edit, `vault.kept_carry()` moves the
  row to the edited file when its inode is unchanged, beside the carry of the language-ID cache. An import that
  replaced the file during the edit has another inode, so its next write keeps a new original.
- A reuse sets the mtime of a `.renewed` file in the keep's time folder to now. The prune ages a time folder from the
  later of its name and that mtime, so the latest write can be undone for the full setting. A keep in its last hour
  (`regrab.KEEP_MARGIN`) is not reused, and the write keeps a new original. A prune that decided before the renewal
  can remove such a keep during the write. The other host's NFS cache can show the old `.renewed` mtime for about a
  minute. Sidecars kept with the video share its time folder and stay with it.
- A failed write after a reuse leaves the keep. `Swap.unkeep()` removes only a keep its own write made.
- Sidecars follow the same rule. A sidecar the hook wrote again reuses its own keep. A copy keep is reused as a link
  is. A conversion that keeps its original records the new path, so a later remux of the new `.mkv` names the old file.
- No migration runs. A file can have several keeps from older versions. Its original is in the oldest time folder
  after the file's last import, while it is still there.

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
  sources are the header, the stream durations of mkvmerge's tags for this file, the stored end, and
  the last packet of the film. The stored end is where the last video or audio frame ends: the last
  Clusters of a Matroska file, read up to any zero padding, the sample tables of an MP4
  (`checks.stored_end()`). A TS file has no
  stored end, because its reader reads the whole file. A container with no reader, as AVI, takes
  ffprobe's stream durations instead, an estimate. A cover picture, see `decide.cover()`, leaves the
  statistics tags. A header that no other source confirms gets a duration alert only.
- **Runtime.** A movie counts as short when it runs under 0.6 of its listed runtime, and long over
  1.4. An edition word such as Extended or Director's Cut widens that to 0.5 and 2.0. A TV movie or
  a stand-up special gets 0.5 and 1.8. A file with several cuts in it is never long. An episode is
  only checked when its listing is 20 minutes or more. It's short under 0.6 of the listing, and long
  over about 1.67, which is 1 / 0.6. A file with several episodes is compared with the longest one,
  and only on the short side. The long side also skips season 0 and release names with an edition
  or multi-cut word. Double episodes, season 0 episodes and extended cuts often run long for real.
- **Year.** The check reads the years before SxxEyy or the first quality word. A season tag can have
  up to 4 digits, as in S2008E08, so a year in the episode title after it doesn't count. It skips a
  year that belongs to a title and a daily show's air date. A year within one of an item year is ok.
- **Episode title.** A release can follow another episode order than Sonarr, so a file can hold
  another episode with the right language and runtime. The title comes from the scene NFO beside
  the file Sonarr imported from, else from the release name between the episode tag and the first
  quality word or release flag, such as REPACK or a language in capitals. A flag in title case is a
  title word, as in "Internal Affairs". In a name all in capitals, only the flags right before the
  quality word are cut, so "FRENCH WEEK" stays. A tag may name one segment
  of a DVD order, as S04E15a. In a name with no quality word, a last "-GROUP" is the group. An NFO
  line with an episode tag, such as a MediaInfo "Title" line that holds the whole release name, gives
  the words between the tag and the first quality word. It keeps a last "-word", because a title such
  as "7-3-1" ends in one. The hook reads the NFO at the event and keeps the title in the job, because a usenet
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
  number then alerts. The release's episode tag plays no part, even when Sonarr's scene numbering
  imported the release to other numbers than its tag. Two releases can share one tag when a show
  restarts its season numbers, and the scene numbering then sends the new episode to an old one. The alert
  names the imported episode with Sonarr's title, then the episode the release's title belongs to,
  in Sonarr's order, with its absolute number for anime.

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
language, averaging 0.8 or more. Otherwise `lang` is null, and `why` names the rule that failed. The package sets
`ORT_DISABLE_TELEMETRY=1` for every helper, so onnxruntime writes no telemetry files.

**What it cannot hear.** Whisper does not know Irish, Scottish Gaelic, Mixtec, Zulu, Xhosa, Quechua
or Kurdish, and it names a neighbour for them. It knows Belarusian but gets it wrong (`WEAK`). When
the tag or the original language is one of these, the module answers null and runs no model. Whisper
also confuses close relatives, such as Galician and Spanish, or Hindi and Urdu. So an answer that is
a relative of the tag or the original language (`KIN`) is withheld.

**Limits.** Hearing a file takes tens of CPU seconds and several hundred MB of memory. The CLI sets
`oom_score_adj` to 1000, so the kernel kills it first under memory pressure. Set
`OOMPolicy=continue`, see "Memory". A machine has `BACKGROUND_WORKERS` turns at the model, the locks
`lid.turn`, `lid.turn.1` and so on, so each background job can hear at once. Each turn loads a model of its own, about
0.8 GB, and lid.py takes the model lock of its turn. The workers of a backfill ask for a turn one at a time, so a hook
job waits for one hearing at most. A hook job that waits holds `lid.turn.gate`, so a background sweep gives its turn
up after its current pair of windows, and the hook job takes the first turn that frees, see "Yield to an import". A
background job in a job process waits for a turn without the gate, so it makes no sweep yield. It takes a turn only
while the gate is free, so a hook job still goes first. A sweep and a background job test the gate with a shared lock,
so neither looks like a waiting hook job to the other. In a hook job all tracks of a file share 120 seconds, cut to
the time left less `LID_RESERVE` for the audio samples. With too little time left, or a file under 60 seconds, the
hook skips detection. No answer leaves the decision as it was.

**Yield to an import.** An import that needs a hearing takes a free turn when one exists. No sweep then yields. When
background sweeps hold every turn, each sweep checks the gate after each pair of windows, about every 4 seconds on a
fast CPU. The first sweep that finds the gate held gives its turn up. The import tries the turns every 0.2 seconds, see
`checks.lid_turn()`, so it takes that turn almost at once. The other sweeps keep their turns. A second sweep also gives
its turn up only when its own check falls inside that short wait. That is rare. Each extra yield costs one model load
of a few CPU seconds. No heard window is lost, because the cache keeps each pair. The job takes the next free turn and
goes on. The limits of `HOOK_WORKERS` and `BACKGROUND_WORKERS` hold the whole time.

**Cache.** `STATE_DIR/lid.sqlite` keys each answer by path, size, mtime, stream, model and duration.
The model name carries its revision, so a model bump misses the old rows. A hit applies the current
thresholds, so a threshold change needs no new hearing. An edit changes the mtime and never the
audio, so the hook carries the rows over after each successful mkvpropedit. The file must still have
its inode. An import during the edit gives another inode, so the new file gets no rows. The mark of
a burn-in job follows the same inode rule, also after a failed mkvpropedit, which can still write.

**Install.** `arr_lid.requirements.txt` pins faster-whisper and its dependencies by hash. The model
is `Systran/faster-whisper-small` at revision `536b0662742c02347bc0e980a01041f333bce120`, in
`LID_DIR/models/small`. `--fetch` downloads it once and checks its sha256. The model loads with
`local_files_only`, so the module never reaches the network. Create `LID_DIR/ready` last. The hook
uses detection only when that file exists, so it never uses a half-built install.

```
python3 -m venv /opt/arr-media-guard-lid/venv
/opt/arr-media-guard-lid/venv/bin/pip install --require-hashes --only-binary=:all: -r arr_lid.requirements.txt
/opt/arr-media-guard-lid/venv/bin/python arr_media_guard/lid.py --fetch --model-dir /opt/arr-media-guard-lid/models
/opt/arr-media-guard-lid/venv/bin/python arr_media_guard/burnin.py --fetch --model-dir /opt/arr-media-guard-lid/models
touch /opt/arr-media-guard-lid/ready        # last
```

## Subtitle text

`text_language()` in `decide.py` names the language of a subtitle text. It needs no model and no
dependency. Each of 18 languages has a list of common dialogue words: English, Spanish, Portuguese,
French, German, Italian, Romanian, Dutch, Afrikaans, Swedish, Danish, Norwegian, Polish, Czech,
Slovak, Turkish, Russian and Ukrainian. A word in one list only is a *telling* word. Its answer is the *read* language.
A word common in two listed languages of one codepage counts in both lists and tells neither, see the comment above
`STOPWORDS` in `decide.py`. It joins the other list only for this read, see `TEXT_SHARED`. The timing checks keep it as
a word to match.

- The letters give the script first. Greek, Hebrew, Arabic, Persian, Thai, Korean, Japanese, Chinese,
  Hindi, Tamil, Telugu, Georgian and Armenian text gets the language of its script. Latin and Cyrillic
  text goes to the lists. Serbian and Macedonian share most Russian stopwords, so one of their own
  letters (ј, љ, њ, ћ, ђ, џ, ѓ, ќ, ѕ) rules out Russian and Ukrainian.
- The OCR of a picture subtitle can write some letters of English text as Cyrillic or Greek letters that look the
  same in a subtitle font (`decide.LOOKALIKES`). A real track held them in 18 percent of its letters: every e, c and
  x, most y, and the capitals A, B, C, E, H, M, O, P and T. When every Cyrillic and Greek letter of a track has a
  Latin look-alike, each reader of its cue words takes those letters as Latin (`latin_lookalikes()`). The readers are
  the text read, the text gate of a removal (`subtitles.tag_read()`), the word check (`subsync.flat()`) and the
  whole-file timing (`align.anchors()`). The rule judges every cue of the track, so one letter with no look-alike
  anywhere keeps all letters as they are. Real Russian, Ukrainian and Greek text holds such letters, as и, л, я, λ
  and π, so it never changes. The text read takes the cues as they are until one holds a look-alike letter before any
  real letter (`real_letter()`). Only then does it read the rest of its cues, so a plain or a Russian track costs no
  more reads. The read of an import takes at most `TEXT_CUES` blocks, so it judges those. A Greek text written only
  in capitals that look like Latin letters (Α, Ε, Η, Ι, Κ, Μ, Ν, Ο, Τ, Υ, Χ) folds, so it reads as no language. A real
  Greek track holds Σ, Λ, Π or a lower-case letter in its first cues. The text of the track never changes.
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
  language go first. Latin credit lines that open an Arabic sidecar make its whole text read as mixed scripts. So when
  the whole text reads as no language, its cues that hold a character outside ASCII read again. The same two reads
  judge a garbled track, see "Garbled subtitle repair". Hebrew bytes decoded as cp1253 give Greek
  letters, but with ς inside words, which Greek writes only at a word's end, so that decode reads as no Greek. When no
  decode reads as a language, the first codepage of the name's language whose decode gives the letters of one script
  wins, in Latin letters mostly ASCII ones. So a Serbian name takes cp1250 for Latin letters and cp1251 for Cyrillic.
  Else the charset is unknown. Each charset decodes the bytes strictly, UTF-16 too. Each later read of the sidecar, by
  the proof, the retime of a conversion and the flash check, decodes it strictly with that charset too. A sidecar that
  does not decode as its charset now fails the proof and gets no new times. A conversion never muxes a sidecar
  whose charset is unknown. It stays beside the new file byte for byte. `repack.sidecars_left` and `repack.sidecars_unread`
  name it, and the "Unreadable subtitles" notice says why. The same decode reads a sidecar beside a Matroska file for the
  subtitle checks, and they leave out a sidecar whose charset is unknown. So no check reads its text, and no fix moves or
  writes it. The decision line names it in `sidecars_skipped`. mkvmerge gets the codepage with `--sub-charset`.
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
repair gives a track its text back when the repair checks itself. Else the track leaves the file, and its bytes go
beside the video, see "Unreadable subtitles". Data a player cannot read right is broken, and AMG removes it unless a
fix is easy and sure.

**Where it runs.** `--sub-check`, `--sub-time` and the deep analysis check every SubRip, ASS, SSA and WebVTT track of a
Matroska file, and every UTF-8 `.srt` sidecar beside it. An import does not. The muxes that garbled text are old, and
old mkvmerge versions wrote no Cues entries for subtitle blocks. So only a read of the whole file reaches their text.
These runs make that read, and an import never does.

**The check.** The cp1252 bytes of garbled text are the sidecar's bytes. `decide.recode()` *reads them back*: it decodes
them as UTF-8 and as each other codepage of `CODEPAGES`, see "Subtitle text". A character that cp1252 does not write,
such as a "♪" a later tool added, stays as it is, and the text around it reads back. A reading counts when every cue
decodes, it changes 1 percent of the letters at least (`GARBLE_SHARE`), and its text reads as a language the codepage
writes. Text in its right encoding changes only in a few borrowed words. Garbled text holds only the letters cp1252
writes. So a cue that holds another letter, such as a kana, a kanji or a hangul, is right text. The check and a repair
keep it as it is, and the other cues read back. A cue read back as Chinese, Japanese or Korean changes only with 3 CJK
letters or more (`GARBLE_CJK`). cp932 reads the cp1252 bytes of "——" as one kanji, and those of "“——”" as two. So right
Japanese text with such cues gets no reading. A garbled track still reads back when some of its cues hold right text.
Latin credit lines that open an Arabic track make its whole text read as mixed scripts. So a reading other than UTF-8
whose whole text reads as no language reads again. Only its cues that hold a character outside ASCII read then. A right
English track with a block of Greek lines reads as English, so its Greek cues never decide alone. A UTF-8 reading
counts with no language too, because text in another codepage holds no pairs such as "Ã©" that decode as UTF-8.
When the text as it stands reads
as a language, or a cut cue holds a letter before its cut, a UTF-8 reading counts only when one character decodes
whole. Right text whose only letters outside ASCII end its cues, as "José" in English or Indonesian dialogue, reads as
UTF-8 cut at each of those cues, and decodes no character. A Hebrew track whose every cue the muxer cut in its first
character, the right-to-left mark, decodes none either, but its text reads as no language, and no cue holds a letter
before its cut. A track of 300 letters or more is
garbled when a reading counts. A UTF-8 reading wins over every other. Else the reading in the language of the track's
tag wins. The check reads the dialogue of an ASS, SSA or WebVTT cue: override blocks such as `{\an8}`, karaoke tags,
the drawing commands after `{\p1}` and WebVTT tags drop out, see `subtitles.dialogue()`. A no-break space stays,
because garbled "à" holds one.

**The repair rule.** A garbled SubRip track is repaired when its reading is UTF-8 and reads as the language of its tag.
Every character of a UTF-8 read-back must decode, so the read-back checks itself. A one-byte codepage decodes almost
every byte. When the muxer lost a byte, the read-back drops or changes letters and nothing shows it. Persian in cp1256
loses letters such as پ, چ and گ that way. A latin-1 muxer leaves control characters that read back as wrong letters.
So any other reading leaves the file. So does a reading that matches no language or another language than the tag,
and a garbled ASS, SSA or WebVTT track, because the proof checks the new text of a SubRip track only. The rule holds
for Latin text such as French, whose garbled words still read as French, because most of them are plain ASCII.

**The word check.** The word check of "Subtitle match" compares the heard words with the cue text, so it cannot judge
garbled text. A garbled track whose words do not match keeps its place: its verdict becomes `unknown` with `held`, and
the repair runs. The track's new times and flash ends wait too. The run then stays pending in the subtitle cache, so the
next run judges the readable text. A track whose read was cut short waits too, but it is never repaired, so it only
alerts and nothing is pending, see "Long subtitle track handling".

**The repair.** One remux of `resub()` writes the new text, and the packet proof checks it. The proof
rebuilds the text from the same inputs, so it shows that the remux wrote the plan, never that the plan
is right. These guards carry that:

- The track's own text is read back from UTF-8.
- A cue cut inside a character gets U+FFFD there and counts as cut. When more than 2 percent of the cues are cut
  (`REPAIR_CUT`), the track is not repaired. The remux counts again over every cue it writes, and a cue that does not
  read back stops it.
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
- mkvmerge writes the remux, with a Cues entry for each subtitle block. An import can read the tracks
  after it. An old mux may cut bytes from the start of each frame and keep them once in the track header
  (*header removal compression*). The proof reads the packets whole on both sides, so it still shows
  every packet the same.
- A repaired track gets no other change in the run, such as new times. A later run judges its times.

**Bytes that are not UTF-8.** A SubRip track can hold bytes that are not UTF-8, as when an old muxer stored a sidecar's
cp1252 "é" as the one byte 0xE9. A player shows a broken character for each such byte. mkvextract and mkvmerge's copy
of the track both write U+FFFD for it, so no remux can keep the bytes, and the track is broken. One such byte is
enough, as for MP4 timed text, see "Conversion". The read of the cues already holds each block's bytes, see
`subtitle_blocks()` and `full_read()`, so the check needs no other read. The track gets a verdict with `encoding` and
no repair, whatever its text reads as, and it leaves the file as below. Its verdict's reason counts the cues that show
U+FFFD. The alert says the track shows broken characters, because some of its bytes are not valid text. Garbled
subtitle repair 3 adds this check. It rechecks no older result, because a recheck reads each checked file whole again.
A backfill with `--sub-check` and the deep analysis skip a file whose saved result still holds. So a file checked
before is found when it or its sidecars change, when a run names it, or while its result is pending.

**A track that cannot be repaired.** A garbled track that is not repaired leaves the file in the same proven remux, as
a removal does (`subtitle_garbled_removed`). The original is kept for `KEEP_ORIGINALS_DAYS`, so with `0` the track
stays and alerts. Its bytes go beside the video in its own format, see "Unreadable subtitles". Before the remux, one
ffmpeg read of the original writes the packets of a SubRip track as SubRip, and `proof.packet_text()` shows each cue's bytes equal to its
packet's md5. The same read writes the packets of an ASS, SSA or WebVTT track as they are. mkvextract then writes the
track's file with its header from the codec private data: the script info and styles of ASS and SSA, and the WebVTT
header, cue settings and identifiers. `proof.event_text()` shows the text of every packet in that file, byte for byte.
A dry run, `SUBTITLES=check` and a track with a cut read only alert. A track the check misses is never touched.

**A garbled sidecar.** A UTF-8 `.srt` sidecar gets the same check, with the language of its name as its tag. A
sidecar with no language in its name takes the language its UTF-8 read-back reads as, and the check of the repaired
text holds it to that. The check reads each cue as the file holds it, so the no-break space of a garbled "à" stays.
The repair rule holds as for a track, with the same proof: the read-back and the check of the repaired text. No cue
may stay cut, because no muxer cut a sidecar's cues, and U+FFFD at a cut would stand where a right letter was. The word check, the
timing and the flash check read a sidecar to repair, as they read a garbled track. A mismatch of its words waits, and
so do its new times, its moved lines and its flash ends. The run then stays pending, so the next run judges the
readable text. A mismatch that another check finds moves the sidecar to the kept originals, as a removal takes out a
garbled track.

`subtitles.side_repair()` writes the text of each cue read back, see `remux.repair_text()`. Every other byte stays, a
BOM and the line ending of each line too. The new text must hold each cue of the old one at its times, with its repaired text, as
the proof holds a repaired track. The original goes to the kept originals, as for a retimed sidecar. So with
`KEEP_ORIGINALS_DAYS` `0` the sidecar stays and alerts. A sidecar that changed since the check stays.

Every other garbled sidecar is renamed, and so is one whose repair fails its proof at the check or at the write. Its
bytes move to the name of "Unreadable subtitles", so a player, the apps and Bazarr no longer read it. The move needs no
kept original. `subtitles.broken_side()` makes a hard link of the sidecar at the new name and checks the link's bytes.
On a share that refuses a hard link, `keep_broken()` writes a copy. The sidecar's name then goes only while it names
the same file, with the inode, size and mtime of that check. So a sidecar Bazarr writes during the move stays, and the
new name goes. The word check, the timing and the flash check leave a sidecar to rename out. A sidecar in another
charset is not checked, see "Subtitle text". Bazarr may fetch a new sidecar after the move, and the next run checks it.

The decision log holds each track's verdict under `garbled`, and `later` on a repaired track whose times or words wait
for the next run. The remux names the repaired tracks in `subremux.recoded`, with the reason `subtitle_repaired`, and
the tracks taken out with their file names in `subremux.stripped`. A garbled sidecar is a `sidecars` entry with the
action `repair` or `rename` and its verdict in `garbled`. A rename holds the new name in `to`. A repair holds the kept
original in `kept`, and `later` when its times or words wait for the next run.

## Unreadable subtitles

AMG keeps the bytes of subtitles it takes out because no player reads them right in one file beside the video. Three
callers use one path, `remux.broken_plan()`, `keep_broken()` and `drop_broken()`: a conversion that removes a broken
MP4 timed-text track, see "Conversion", a subtitle remux that removes a garbled track or a SubRip track with bytes
that are not UTF-8, and the move of a garbled sidecar, see "Garbled subtitle repair". A subtitle remux that removes a
text track that does not match the audio keeps its bytes the same way, under its own name, see "Actions" in
"Subtitle match". So do a conversion that leaves out such an MP4 timed-text track, and the rename of a sidecar that
does not match.

**The name.** The file is `<name>.<language>.broken-encoding.<format>.txt`, as `Film (2001).per.broken-encoding.srt.txt`.
The language is the track's tag, or the language part of a sidecar's name. A track with no tag has no language part.
The format is `srt`, `ass`, `ssa` or `vtt`, the track's own. Players skip a `.txt` file. Bazarr skips this name,
because it does not match the video's. Sonarr and Radarr count `.txt` as a subtitle extension, so a rescan records
the file, and Radarr may rename it with its own language part when it renames the movie. AMG's own sidecar scans read
`.srt` files only. AMG never writes over a file. A file with the same bytes stays, as when the
same release converts again or Bazarr fetches the same sidecar again. When a file of other bytes holds the name, or
two entries share the language part and the format, the name takes a number in front of `.broken-encoding`: the
track's number, as `Film (2001).per.3.broken-encoding.srt.txt`, or 1 and up for a sidecar. The file of a track that
does not match ends in `.mismatch.<format>.txt` in place of `.broken-encoding.<format>.txt`, as
`Film (2001).eng.mismatch.srt.txt`, and numbers the same way. No two entries take one
name. A dry run names each file before the bytes are read, so a name a file holds counts as taken there. So does a
name that another plan of the run gave, as a track's text and a sidecar of one language, so a dry run names one file
once, as an apply does. A subtitle
remux plans the same way before its remux. A garbled track, or a track that does not match, whose names all hold files
stays in the file then, and the alert says why. The remux makes its other fixes.

**The write.** `keep_broken()` writes the file with the owner and mode of the file it came from, see `own_like()`, and
reads it back. It creates the file with that mode, as `new_tmp()` does, so a share that refuses chmod keeps it. It writes after the proof and the swap's checks, and before the rename, so a failed write keeps the
original. When a later step fails, or an undo puts the original back, `drop_broken()` removes each file it wrote. A
file it reused stays. The entry of each file holds `sidecar`, `reused`, `written` and `removed`, and the decision line
keeps them. A subtitle remux names each file in `subremux.garbled_text`, or `subremux.mismatch_text` for a track
that does not match, and a failed one the files it removed again in `subremux.garbled_text_removed`.

**The notice.** `report.unreadable_finding()` builds the one alert, "Unreadable subtitles". It names each track as
track N of the original file and each sidecar by its name, with the file that holds its bytes. A sidecar that a
conversion left beside the video as it is goes by its name alone, see "Conversion". Its charset is unknown, mkvmerge
reads it otherwise than a player shows it, or it holds ASS or SSA text under a .srt name. Its own file still holds its
bytes. Several such sidecars share one pair of sentences, and then a sentence per reason names them. `decide.KEPT_OUT`
holds one row per reason: the words of its decision line and its sentences in the notice. When AMG reads
each sidecar of the alert well, and mkvmerge misreads it, the title is "Subtitles left out of the MKV". The alert marker
keys on the kind
`unreadable`, see `logs.alert()`. A conversion's alert
starts with the conversion. A run posts one such alert, and it also says each repair of the run, see
`subtitles.sub_findings()`. When a run converts the file and then takes out garbled tracks or renames sidecars, the
alert names the conversion first, then the tracks taken out after it, see `process.after_edit()`. Such a track goes
by its number in the original file when it came from one of the original's tracks, see `process.conversion()`. A
track the conversion added, as a muxed sidecar, goes as track N of the converted file. It posts with `DISCORD_POSTS`
issues, from the deep analysis and a recheck too. With `all` it is the only post of a conversion and of the repairs.

## Subtitle match

A text subtitle can hold the lines of another episode or another cut, and so can a sidecar. `subsync.py` checks a
text subtitle against the audio. The owner's rule is "rather no subtitle than a wrong one".

**What it checks.** A SubRip, ASS, SSA, WebVTT or MP4 timed-text track in a full, SDH or dub role, a `.srt` sidecar in a
conversion, and a `.srt` sidecar beside a Matroska file, where a program such as Bazarr writes its downloads. Its language must be the
language of a main audio track. The check hears the track that plays when it speaks that language, else the first main
track that does. A mismatch then hears the other audio tracks in its language, see "A subtitle of another audio
track". A forced, commentary or picture track, a forced sidecar, a track in another language and a file under
5 minutes are never checked. Nor is Japanese, Chinese or Thai text, which has no spaces between its words, so the
check could never give a verdict. The check of a conversion takes the duration ffprobe reads, because mkvmerge gives an
MP4 none. So a file with no such track or sidecar costs nothing. The check runs on imports and in
the jobs a bare backfill queues. A flag backfill runs it only with `--sub-check`. `SUBTITLES` sets what an import does with it: `off` runs no check, `check`
reads, reports and alerts and changes nothing, `fix` acts, and `deep` (the default) adds the deep analysis. An unknown
level acts as `check`. `--sub-check` and `--sub-time` ignore `SUBTITLES`, because a person asked for them. Without
`--apply` they report, and with it they fix. It needs language detection, see "Audio language detection".

**The read.** `subtitle_cues()` reads every cue with its start and end, by the Cues, as `subtitle_read()` does. A cue
ends at its BlockDuration, else after the DefaultDuration. A cue with neither ends where the next block in time order
starts, the RFC 9559 rule for BlockDuration. An empty frame gives no cue, as ffmpeg, mkvextract and mkvmerge drop it.
Its block still ends the cue with no duration before it, so that cue keeps its end. A remux writes that end. A
second cue entry of one track for one block is left out, so each block reads once. Both entries name the same Cluster
position and position inside it. The last cue with no duration and no block after it has no known end. It ends at its
start in the read, and the track gets no plan of new ends or times (`remux.Unended`), because the plan would make one
up. The decision record names such a track in `unended`. A whole-track fix still moves every cue. A sidecar and an MP4
track are read as SubRip text. A track the Cues do not index comes from the walk of every Cluster, see "Frame times".
Each of its blocks is read at its byte as a block the Cues index.

**Long subtitle track handling.** It bounds the read of a long track. The read stops at 100,000 blocks a track
(`CUE_MAX`, and `PICTURE_MAX` for PGS and VobSub), or after 2 minutes (`READ_WALL`) on a slow share. A typeset fansub
track, or a PGS track redrawn every frame, can hold tens of thousands of blocks, so a lower cap cut real tracks short.
The whole-file read of a track the Cues do not index keeps the same cap, and its block reads stop after 30 minutes (`FULL_WALL`). The
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
file has a text subtitle, and a window inside a kept sample is cut from it, so no audio is decoded twice. Each window
and each sample decodes on the file's clock (`ff.clock()`, see "Time reads"). A window that starts inside a late start
or a gap hears silence there, so its word times match the cues. Before, such a window started at the first packet
after its start, and its words read early by up to the delay. The words and the kept samples cached before read
again once, because their keys name the clock. When the
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

**A subtitle of another audio track.** A file can hold more than one audio track in the subtitle's language: a
commentary, or a second main track with its own words. A subtitle of that track can have no title and no commentary
flag, so it reads as a full subtitle, and the check compares it with the main track. On real files such a track did
not match and left the file: an untitled commentary subtitle beside a titled commentary audio, and a subtitle of the
second of two untitled main tracks. So before a mismatch counts, every other audio track in the subtitle's language
hears the same windows as the first hearing, with the same 24-second window for a window that hears too little
(`subtitles.sub_others()`). The language is the one the decision gives the track: its tag, else a language its title
or its hearing names. So an untagged track titled "English Commentary" hears, and an untagged track with neither
never hears. One hearing serves every mismatch of that language. A match on any of them makes
the mismatch unknown: the track stays with no change and no alert, as a translation does. When that hearing gives no
answer, as when no time is left or a stop was asked, the mismatch is unknown too, and the track stays. No hearing
starts after a stop. The decision record logs each such hearing in `others`, with its verdict, its windows and
what it cost (`took`, `cpu`, `cached`, `waited`). A mismatch that no other track matches counts. The rule runs
after the translation rule, so a mismatch that rule holds pays for no hearing. It holds for sidecars and in a
conversion too.

**Speech in another language.** A right subtitle also translates speech in another language than its own, as in a film
with scenes in Arabic. Whisper, told the subtitle's language, hears few of its words there, so such a window reads as
a mismatch. A right English track of a film with Arabic scenes left the file that way. So before a mismatch counts,
one lid.py run names the spoken language of each window that counted (`subtitles.sub_spoken()`, `lid.tongues()`,
`--langs`). Each window needs 4 seconds of speech (`lid.WINDOW_SPEECH`), and a read under 0.6 names no language, as
for a sample. A window whose speech reads as another language than the subtitle's does not count. With under two
windows left, the mismatch is unknown: the track stays with no change and no alert. So is a mismatch whose read gives
no answer, as with no time left or a stop, as for the hearing of another audio track. Each window logs its read as
`lang` and `prob` in the decision line. The read runs on mismatches only, before the hearing of the other audio
tracks. A window in another language stays out of that hearing and its check (`subtitles.sub_others()`), so it pays
for no other hearing and never turns a match there into unknown. Nothing is cached. On 96 real English word-check
windows of 10 seconds, 17 held under 8 seconds of speech and none under 4, and every one read as English (one at 0.47
named none). On 84 windows with 4 seconds of speech of 12 episodes in 9 other languages, 80 read the track's language,
3 named none and 1 read English.

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
  extracted text, the files of single tracks and the proof's reads, go into a folder under `STATE_DIR`, never the
  system temp dir, which is often a small tmpfs. The trim, the damage read, the conversion and the video windows keep
  their work files there too. A worker that starts removes a work folder that a killed step left over a day ago.
- A track that does not match leaves only when its text reads as its tag. On real files an English-tagged track that
  held Dutch text, and a right English track in Cyrillic look-alike letters, did not match and left. Such letters
  now read as Latin, see "Subtitle text". So the check
  first reads the text of each such track: `decide.text_language()` of its first 200 cues, the blocks
  `subtitle_read()` reads, from the cues the word check read already (`subtitles.tag_read()`). The decision record
  logs the read in `text`. A track whose read names another language, or none, stays in the file with its flags off,
  as with `KEEP_ORIGINALS_DAYS` 0 below, and the alert says that its text doesn't clearly read as its language. The
  alert says "Turned off their default and forced flags." only when AMG turned one off: the run, or for a deep analysis
  also its import, whose held alert keeps those tracks in `flags`. A track that had neither flag on gets no sentence on
  its flags.
  `subremux.kept_for` names it with `text`. `text_language()` never names a language with no word list, such as
  Finnish or Hungarian (owner 2026-10-25). A track with such a tag leaves when its read names no language, and stays
  when its read names another one, as Dutch text tagged Finnish does. On a census of 300 files, 1,884 of 1,982 text
  tracks in a word-check role may leave on a mismatch this way, and Dutch (16 of 55), Danish (13 of 54) and
  Norwegian (8 of 29) keep the most. A text too short to judge gives no sign, so its track stays. A picture track has
  no text to read, so it stays too. In a conversion such a built-in track stays in the new file, and the check of
  the new file turns its flags off.
- A text track that leaves has its bytes written beside the video as `<name>.<language>.mismatch.<format>.txt`,
  through the path of "Unreadable subtitles". So its text outlives the kept original. `subremux.saved` names each
  file by the track's place, and the alert names it. When every name for it holds a file, the track stays with its
  flags off, and `kept_for` names it with `names`. A conversion that leaves out an MP4 timed-text track writes its
  text beside the new file the same way (`convert.mismatch_texts()`). It reads the bytes of each sample, and the
  times move by the start shift of the new file. Another text track, one whose bytes do not read and one whose names
  all hold files stay in the conversion, and the check of the new file takes them out with their bytes or turns their
  flags off. No picture track leaves for a mismatch: the word check reads text tracks only, and the text gate keeps
  a track with no text. So no file is ever written for one.
- One mkvmerge call writes the remux, as in the header repair. Up to 2.17.0 ffmpeg wrote it, with each rewritten track
  as an input of its own. Beside a sparse copied track, such as a forced track of three cues, ffmpeg held that input
  back for minutes. Its packets then sat far from their times in the file, and players never showed them. mkvmerge
  writes each packet by its time. It reads each input in file order, though, and holds back only about 20 MB of it. A
  track that the source stores late would stay late. So the subtitle tracks it copies come from a second input of the
  same file, which mkvmerge reads on its own. The chapters, tags and attachments come from the first input only.
  mkvmerge compresses PGS and VobSub tracks with zlib by default, and some hardware players read none. So a track that
  the source holds uncompressed stays uncompressed. It copies every other track with its UID, language, BCP 47 tag, name
  and flags, and the file's attachments, chapters, tags and Segment UID (`--segment-uid`). `--track-order` keeps the
  order of the tracks. A track that has no BCP 47 tag gets the one of its legacy language, and that passes. A track
  that has only a BCP 47 tag gets the legacy tag that mkvmerge writes for that language (`eng/it` becomes `ita/it`).
  That passes when `decide.track_tag()` reads the same language from both pairs, as for `it` and `zh-Hant`. mkvmerge
  writes `chi` for `yue` and `cmn-Hant`, `may` for `zsm`, and `qaa` for `qaa`. These tracks read `yue`, `cmn` or
  `und` before the remux and `chi`, `may` or `qaa` after it. So the track list changes, and AMG refuses the remux.
- Two `--engage` options of mkvmerge keep the copied packets as they are. `keep_whitespaces_in_text_subtitles` keeps
  the spaces at the ends of each line of a text subtitle, which mkvmerge trims by default.
  `dont_normalize_parameter_sets` keeps each HEVC keyframe as it is, where mkvmerge would put the header units in
  front of it. mkvmerge writes each line break of a SubRip packet as CRLF, so the proof compares SubRip packets with
  each CRLF as LF, see below. It also drops the NUL byte that HandBrake puts at the end of each ASS event. So the
  proof compares each ASS or SSA packet without that one byte, and changes nothing else. An old mkvmerge put each
  Dolby Vision RPU (NAL unit 62, the metadata of a frame) at the start of the next packet. mkvmerge moves it to the end
  of the frame it describes. The proof then reads the NAL units of that HEVC track in both files, never
  through a filter, and passes that move alone. Each packet stays as it was, or loses its leading RPU and gains the
  next packet's at its end. The times, the keyframe flags and the units joined across the packets stay byte for byte,
  so no RPU is lost or doubled. Any other change refuses, and the refusal names the Dolby Vision RPUs.
  `--disable-lacing` writes each audio frame in its own block, as the conversion does. A player that seeks to 0 skips
  each block that starts before the first video keyframe. A lace of AAC audio there can hold 170 ms, and a block of
  one frame holds 21 ms. An audio track that mkvmerge would time on its own gets its planned times, see "Planned
  audio times".
- ffmpeg first writes each retimed track alone into a file of its own, with the `setts` filter, so its cues move to
  (time - offset) / rate, and a ratio scales each cue's duration too. `setts` works in milliseconds, so each moved time
  rounds once. In the time base of a source finer than 1 ms, it rounded to that tick, and the muxer rounded again. A
  cue then sat up to 0.5 ms plus half a tick off the fix, and the proof refused it. One ffmpeg read writes every
  retimed track. A file of one stream keeps its packets in time order. ffmpeg's muxer sets a time that goes back in
  the file to the time before it, so a track that the source stores out of time order would get cues seconds late.
  ffmpeg logs a step back of more than 2 ms as a warning, and the remux refuses on it before the merge: "subtitle
  track 13 stores its packets out of time order, so ffmpeg cannot move it". It logs a smaller step at debug level
  only, and the proof refuses that move, see "The proof". With `--sync` on the second input, a track that a fix
  moved earlier than 20 MB of other subtitle data stayed late. A UHD remux can carry 1.5 Mbit/s of PGS, about 20 MB in two minutes.
  mkvmerge also drops a cue whose time goes below 0. A cue that the fix moves before 0 starts at 0, and a cue that ends
  before 0 keeps a length of 1 ms, so it never shows. With a length of 0 the proof refused the remux, as when a recap of
  several cues moves before the start. The file keeps the track's own tags, such as a SOURCE_ID. The proof holds each
  cue's length to its start and end as the fix moves them. A ratio also changes the stop command inside each VobSub
  packet, which mkvmerge sets from the cue's new length. A cue moved before 0 changes it too. The proof compares the
  packets of a retimed VobSub track with their delays at 0, and holds each delay to the old one over the rate, within
  one unit of 1024/90000 s. The stop delay of a cue moved before 0 must be its length from 0 to the original's end moved
  by the fix. A track with new times, new ends or new text comes from a file of one track too, see below. mkvpropedit
  gives each such file the original track's UID, and mkvmerge keeps it. The language, name and flags come from
  mkvmerge's options. Any line that mkvmerge prints fails the remux, as in the header repair.
- mkvpropedit sets no property of the new file. Up to 2.17.0 it set every property again after ffmpeg. The track list
  grew, mkvpropedit moved it past the Clusters and chained a second SeekHead. That breaks RFC 9559 6.1 and 6.3, and
  MediaInfo showed no tracks. It only gives a planned audio track its DefaultDuration back, a few bytes of Tracks in
  place, see "Planned audio times". The proof's layout check reads the file after that edit. The new file must keep
  the type, codec and language of each kept track in order, its UID and its BCP 47 tag. It must also keep the
  properties in `KEEP_PROPS` (its name, flags and codec private data), the count of attachments and the chapter
  entries of each edition. mkvmerge
  keeps the codec private data byte for byte, the HEVC hvcC record too, but for the headers it writes again. Only the
  rows of "mkvmerge rewrites" pass, see `remux.header_kept()`, and any other change refuses the remux. mkvmerge also
  writes the Channels element
  of an old E-AC-3 7.1 or AAC 7.1 track as 8, and a DefaultDuration for AC-3 that ffmpeg wrote. Those match the
  streams, and `KEEP_PROPS` holds no audio field. The proof of "Conversion" must pass with the retimed tracks' times
  as the fix moves them. It also holds each stream to its own start time, so a remux that moved every stream fails.
  The file keeps its owner and mode, and the original is kept as in "Kept originals".
- A removal cannot be undone without the kept original. So with `KEEP_ORIGINALS_DAYS` 0, or when the remux or the
  proof fails, the track stays in the file. It gets the role `unmatched` instead, which no policy lists, so it never
  becomes a default. It loses its default and forced flags with mkvpropedit (`subtitle_audio_mismatch`), and the alert
  says why it stayed. A retime of another track in the same file keeps that. `SUBTITLES=check` keeps the times, the
  tracks, the sidecars and the flags, and alerts.
- A sidecar beside a Matroska file that does not match gets the name `<base>.<lang>.mismatch.srt.txt` beside the
  video, with a log line and the alert (`subtitles.side_rename()`). It takes the path of a garbled sidecar's rename,
  so its bytes stay and players skip it. When every name, numbered 1 to 99, holds a file, it moves into
  `.<NAME>-originals` as a kept original. The rename runs only where the move would.
  A sidecar whose times need a fix is written again with new times, and its original is kept the same way.
  Only its time lines change. Every other byte stays, so its text keeps its character set, even one the detection
  misses (`subtitles.raw_srt()`). With `KEEP_ORIGINALS_DAYS` 0 the sidecar stays as it is and alerts. A sidecar with
  a timing line that AMG reads as text of the cue before, with text of its own after it, stays as it is and alerts, see
  `proof.glued_cue()`. A player shows that text as a cue of its own, and a rewrite would keep its old time. The program
  that wrote it, such as Bazarr, can download the same wrong file again, and the hook does not tell it. A later check
  renames it again.
- In a conversion, a sidecar that does not match is not muxed. It gets the same name, or with every name taken moves
  into `.<NAME>-originals`, and with `KEEP_ORIGINALS_DAYS` 0 it stays beside the file.
  A built-in track that does not match is left out of the remux, and
  the proof leaves it out too. The conversion then keeps the original in `.<NAME>-originals`, as a forced conversion does, and
  the alert names it. With `KEEP_ORIGINALS_DAYS` 0, or no place to keep it, the track stays in, and the check of the new
  file turns its flags off. A sidecar whose times need a fix is muxed with new times, and its original is kept. A
  built-in track whose times need a fix gets them after the conversion. The check of the new file reads the same
  windows and the words carried over, so it hears nothing again. The conversion checks a built-in track only when
  the k-th subtitle stream of ffprobe is the k-th subtitle track of mkvmerge, see `convert.sub_streams()`. A CEA-608
  caption stream has no mkvmerge track, so it counts in neither.

**Cost.** The target is 15 CPU seconds per file with a track to check, the model load included, and nothing for a
file without one. A third window costs one more Whisper run in the same process, only for a file whose window heard
too little. A middle window costs one more hearing, only for a track that needs a ratio fix, and its window of 24
seconds one more Whisper run in that hearing. A drift hearing, the hearing at a matched window's offset and a longer
window at too few cues cost one more hearing each, only when the check asks for them. A check makes at most five
hearings after the first, so a ratio from the hearing at an offset still gets its middle window. A hearing starts only
when 10 seconds of the check's time are left. More threads cost more CPU time than they save in wall time, so the
check runs one thread. A failure or a timeout gives unknown and never stops the import. The check shares the job's
time limit, 90 seconds at most. A mismatch costs one more hearing for each other audio track in its language, only in
a file that has one. Those hearings get the check's time again, 90 seconds at most within the job's time limit,
because on a real file the first check took 70 of its 90 seconds.

**Cache and backfill.** `lid.sqlite` also holds each file's saved result. It holds the version and the findings of each
check the run made, the app, the run, and the size and mtime of its sidecars. It has a mark when it asks for an action
that no apply made yet. A planned or failed flag edit is such an action. A flag edit that a hard link or a read-only file
refuses asks for none. A result that asks for nothing never replaces a result of a run with
more checks that still holds, so the import check of a backfill job keeps a deep analysis. A backfill with `--sub-check` skips a file whose result is saved for its size and mtime and its
sidecars, comes from a run that makes every check of `--sub-check`, has no stale check, and asks for nothing more. So a
stopped run goes on where it stopped, an apply after a dry run still acts, and a new sidecar from a program such as
Bazarr is checked. `--sub-check` also takes a file with a sidecar that the flag backfill leaves out, and `--recheck`
skips no file. The decision log holds each result in `subcheck`, the remux in `subremux` and the sidecar actions in
`sidecars`.

A check is stale when its registry entry says a newer version can fix its findings, see
[development.md](development.md#when-a-change-can-fix-old-files). After an update, `runner.queue_rechecks()` queues a
recheck job of each stale file in the background queue, behind the imports, the burn-in jobs, the whole-file checks
and the deep analyses,
and ahead of the jobs a backfill queued. A recheck makes the checks of the run that saved the result, and none deeper.
Garbled subtitle repair 2 checks `.srt` sidecars and ASS, SSA and WebVTT tracks, and takes out a track that reads back
only from a one-byte codepage. A track version 1 repaired in place from such a codepage is UTF-8 now, so the recheck
finds no reading in it. It stays as it is, with any wrong letters of that repair. A saved result names no codec, so every result of version 1 of `--sub-check`, `--sub-time` or
the deep analysis is stale once, and the first start after the update queues one recheck of each such file.

**Flash cues.** A release can store right starts with ends a tenth of a second later, so a player flashes each line.
The check reads the CueDuration of each subtitle entry from the Cues, which mkvmerge and ffmpeg write, so it reads no
block for this. A track that flashes is read in full. It gets a plan only when each entry gave a cue or an empty
frame. A text track or `.srt` sidecar whose median cue shows under 0.5 seconds, in any language and role,
forced too, flashes. Only a cue with a visible character counts, so a cue of tags or of a zero-width space is left
out, and the track needs 20 such cues. Half its short cues must also end over two frames before the next cue starts.
A sign drawn frame by frame, or a karaoke line, runs straight into its next event, so it never flashes. Right text
tracks show their median cue for a second or more, and a flash track for about a seventh of a second. Only a cue under
0.5 seconds gets a new end: the next cue's start less two frames (0.083 s), at most the start plus twice the reading
time, 3 seconds at least and 7 at most. The reading time counts the visible characters, 17 a second. A flash fix never
shortens a cue. A built-in track gets its new ends in the remux of "Actions". A SubRip track goes into a Matroska file
of its own, each packet as it is at its new times, and mkvmerge reads that file. mkvmerge's SubRip reader drops the
blank lines at the start of a cue, and mkvmerge 8 stored them as line breaks, so the packets never go through SubRip
text. For an ASS or SSA track, mkvextract writes its text, the ends go in, and mkvmerge reads it back, which keeps
each packet and the ASS header byte for byte, with the changes below.
HandBrake ends each ASS event and the ASS header with a NUL byte, and writes no `[Events]` section into the header. The
round trip drops those NUL bytes and adds the section. The proof compares each packet that ended in a NUL without that
one byte, and names their count in `nul_cut`. The header must be the one that the ASS row of "mkvmerge rewrites" gives.
Each ASS packet starts with its ReadOrder, the event's place in the script. mkvmerge numbers the
events from 0 in that order. HandBrake may skip a ReadOrder. So the proof gives each packet of the original the rank
of its ReadOrder. The rank applies only when every ReadOrder of the track is a whole number written in digits alone,
and no two are equal. Otherwise each packet must keep its ReadOrder. The proof names the count of packets that changed
in `read_order`. The order of the events stays. `--engage keep_whitespaces_in_text_subtitles` keeps the spaces at the
ends of each line of the round trip, so a space at the end of an event stays, and the proof holds it.
mkvmerge stores each line break inside a SubRip cue as CRLF, in a timed track and in a copied one. The mkvmerge that
made a source may have stored LF. So the proof compares each packet of every SubRip track with each CRLF as LF, in both
files, and names the count of packets that changed in `crlf`. Any other change of a byte fails. mkvmerge writes U+FFFD
for each byte of a SubRip packet that is not UTF-8, in any remux, and warns. So a run that reads whole files takes
such a track out, see "Garbled subtitle repair". An import never makes that check. Any remux it plans for such a file
fails on the track, so its subtitle fixes wait for a run that reads whole files. Its flag edits still land, and
mkvpropedit makes them when the remux fails.
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
to its stop command, and it ends 10 seconds after it starts at most. A last cue that no set, stop command or
block ends has no known end. The fit leaves it out, and the fix still moves its block. A PGS block is one small
read of its first bytes.
The fixes go into the one remux with the word check's. A PGS or VobSub track moved by ffmpeg's `setts` filter passes the
proof, and a ratio also scales each cue's duration. With no reference, nothing is read.

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
  come from them. For those tracks `--sub-check`, `--sub-time` and the deep analysis read the whole file once, with
  the walk of every Cluster, see "Frame times". The walk gives each block's byte, time and duration. Each block of
  those tracks is then read at its byte, as a block the Cues index, and a PGS block gives only its first bytes,
  because its pictures can take GBs. So the read writes no file and needs no free space. ffmpeg's SubRip copy changed
  a time that goes back with no message, and it ended a cue with no BlockDuration at its start. The walk keeps the
  stored times. A SubRip cue of two lines keeps its line break, as a cue the Cues index does. ffmpeg's SubRip copy
  joined the lines with a space. A WebVTT track now reads too. ffmpeg's copy gave no cue of it. `--sub-check` and `--sub-time` read under the shared file lock. The deep analysis reads before it
  takes the lock, since a film takes a minute. The read is kept by the file's size and mtime, so a file that changed
  meanwhile is read again under the lock. A deep analysis that yields keeps the read in the store for its next run,
  and a track whose read was cut short stays marked as cut there. A next run of another AMG version reads the file
  again, as the read of each version can differ.
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

  The hearing runs on the CPU of the container, with one Whisper thread, so its CPU time is about its wall time. On
  an Intel i9-13900H in October 2026, a deep analysis took a median of 15.6 minutes per hour of video. Three in four
  took under 17.1 minutes per hour. The slowest took 29.1. Each heard window cost about 1.9 CPU seconds there. An hour
  of steady dialogue holds up to about 480 heard windows, so the time follows the amount of speech. An older CPU
  without AVX-VNNI, the instructions for fast 8-bit integer math that the int8 model uses, took about 6 seconds a
  window. That was an AMD Ryzen 5 2600X in a VM. The deep analysis of 2.7.0 spent about 75 CPU seconds on an episode
  in time.
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
     has one, because the dialogue is there, and else the mono mix of `-ac 1`. Then `ff.clock()` fills a late start and
     a gap with silence at the track's own rate, so an onset right after one counts. At 16 kHz about 3 percent of the
     onsets of a healthy film came out other, so the onsets keep the rate. The `-t` of a part bounds the fill. Only an onset after a silence of 0.5 seconds or
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
  trip of the flash fix, and no fix moves it again (`subtitle_blocks_retimed`). The proof holds each start and end to
  the plan. A sidecar is written
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
stops with one line that names the settings. So do the audit's pruning and the subtitle hunter. A backfill with no
instance skips an app the user did not set up.

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
thresholds (`subsync.voiced()`). Spans less than 0.5 seconds apart join. The decode runs through `ff.clock()`, a mix to
mono 16 kHz, then ffmpeg's `aresample` with `async=1:first_pts=0`. It puts each decoded frame at its time from the file's start, and it fills a late start and
a gap in the track with silence. So the spans run on the file's clock, as the cues and the speech onsets do. Before,
the spans moved by the start that ffprobe gives the track. ffprobe gives the file's start for a track whose first
packet lies past its probe, as for made-up tracks 5 and 22 seconds late. The speech then read early by the delay, and
right subtitles seemed late by it. A gap in the track moved every later span early by the gap. The cache key names
the clock, so spans cached before read again once. `lid.sqlite` caches the spans by path, size, mtime, stream, those settings and the
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
match": the track leaves the file in the one remux, and a sidecar gets its `.mismatch.srt.txt` name. The track leaves
only when its text reads as its tag, from the cues the layout check read. A picture track has no text to read, so it
stays with its flags off.

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
some subtitle has a fix or an offset that gets no fix. In each half of the file, at least 3 cue starts must have an onset where the fix puts them,
0.1 seconds after the start. That count must be twice the count where the cues sat, and rare by chance: a chance of
0.001 at most. A segment of the whole-file timing also needs 20 percent of its lines. A whole track does not
(`subsync.LAYOUT_ONSET_SHARE`), because under a music bed silencedetect marks few onsets. Verified real shifts of about
1 second drew onsets at 9 to 19 percent of their starts in a half, at a chance of 1 in a million or less, and the share
rule left them off. A half that confirms where the cues sat, or too few onsets, leaves the times as they are. The onsets
confirmed most fixes of shifted right tracks, and no fit of another episode's track.

The onsets judge an offset that gets no fix the same way, at the ratio of the fit. When no half confirms the offset and
a half confirms where the cues sat, the second clock puts the lines in time. Then nothing alerts, and the decision log
holds the offset under `disagreed` in `timing`. Else the alert says the subtitles seem late. A fix that the onsets do
not confirm follows the same rule. On a film whose speech spans read 22 seconds early, two right tracks alerted as 22
seconds late. The onsets put the lines of one where they sat, and the other got no fix and so no onset read.

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

**Cost.** A fix or an offset with no fix adds the onset read, about 1 CPU second a minute of the up to 20 minutes it
reads. The votes and the
checks cost under a second a track.

## Burned-in subtitles

A *burned-in* subtitle is drawn into the video frames. `burnin.py` finds dialogue subtitles in the picture, and its
docstring describes the method. AMG runs it in the language detection venv through `checks.lid_cli()`, the same way it
runs `lid.py`. So each run gets its own process group, a timeout, and the cleanup of the onnxruntime log. An answer that
holds an error raises, see `process.burn_run()`. `process.burn_ready()` gives the reason when the check cannot run. The
reason is a missing language detection install, a missing `burnin.py`, or a text model in `LID_DIR/models/ocr` that
`burnin.ready()` finds missing or wrong. While `BURNED_IN` is on, the listener's start check and `--selftest` warn with
that reason. `BURNED_IN=off` runs nothing, `check` only alerts, and `fix`, the default, also makes the changes below.

**Quick check.** Every import with a video track, in any container, runs `burnin.quick()` after its own checks and
edits, see `process.quick_check()`. It listens to the audio track that plays first after the edit. It takes 10 windows
of 30 seconds, or 19 when the first ones hold too little speech, and looks at the keyframes inside them. It sorts the
file as clean, flagged or unsure, and changes nothing. A clean file is done. A flagged or unsure file gets a burn-in
job. HDR video, with a PQ or HLG transfer, counts as unsure when it looks clean, because the check misses white text on
it. An MKV file without cues is unsure at once, with no window heard, because every seek in it reads the file up to
that point. The check gets the time the metadata checks left of their 300 seconds, and stops there. With less than
`QUICK_SECS` (90) left, or when the run fails, the file is unsure. The decision line keeps the result in `burned_in`.
mkvmerge gives no duration for an AVI file and no tracks for an ASF or WMV file, so the check gets them from ffprobe
within the same time limit, see `process.burn_probe()`.

**Burn-in job.** The job carries the import's decision inputs, the audio track the quick check used, and in `grab` the
fields of the import job that a re-grab needs. It runs `burnin.full()` without the file lock, so no import waits for
it. It yields when an import job waits, between parts of its speech read and after every 10 frames. A full check stops
after `BURN_TIMEOUT` (3600) seconds. The speech spans come from `lid.speech()` and its cache, which the deep analysis
shares. `runner.burn_plan()` turns the verdicts into a plan, and `runner.burn_step()` decides what the job does with
it. The job reads the audio language from the track's tag in the file as it is now, so a language the import only
heard never turns subtitles off. When the first pass finds a full burn-in and reads its text, a second pass,
`full(second=True)`, looks at frames the first pass did not use. The second pass runs at `check` too, so `check` alerts
on what `fix` would act on. When an import arrives during the second pass, the job keeps the first pass in the queue,
and its next run makes only the second.

| Verdict | At `check` | At `fix` |
| --- | --- | --- |
| none, partial or unsure | log | log |
| full, English, audio English | log | log |
| full, English, audio untagged | alert | alert |
| full, language unknown | alert | alert |
| full, English, audio in another language, both passes agree | alert | turn off the English subtitles, and alert |
| full, another language, both passes agree | alert | a re-grab |
| full, the second pass disagrees or cannot tell | alert | alert |

**English subtitles off.** The job reads the tracks again under the file lock, which is exclusive at `fix`. It turns
off the default and forced flags of every English subtitle track that is on, through `process.edit()`, with its undo
line, its hard link check and its verify. The edit's `recheck` plans the file again with the import's own decision
inputs, which the job carries, see `process.replan()`. That plan leaves out the edits that would turn English subtitles
back on, so the nightly audit counts no further edit for them. A file that is not Matroska keeps its flags, and the
alert says so. A file with no English subtitle track on still gets an alert. The deep analysis and rechecks keep these
flags, as they keep every flag. When an edit fails and no flag changed, the alert says the original file was not
changed. When it fails but still changed a flag, the alert says which tracks play now. mkvpropedit's own words stay in
the decision log.

**The mark.** Before the edit, the job marks the file in the store's `burned_in_muted` namespace with its path, inode
and size, see `runner.muted()`. When the edit fails and changes nothing, the job drops the mark. While the mark matches
the file, a flag backfill, `--sub-check --apply` and `--sub-time` skip every edit that would turn an English subtitle
on, see `process.muted_plan()`. Their decision line says so in `flags_kept`. The class of that line lists the edits that
remain, and ends with "English subtitles kept off by the burned-in subtitle check". When AMG edits the flags or remuxes
the file itself, the mark moves to the new inode and size. A new file at the path, such as an upgrade, has another
inode or size and clears the mark. The nightly audit leaves the English subtitles of a marked file out of its further
edits. The mark is the one exception to the rule that the only English subtitles stay on, see "Safety self-checks" in
[development.md](development.md).

**Re-grab.** When both passes read full text in another language, `regrab.regrab()` runs with kind `burned_in`, under
the exclusive file lock. The second pass ran before the lock, and it counts as the second check. `REGRAB`, `REGRAB_CAP`,
the kept copies and the restore work as they do for an import. Without `burned_in` in `REGRAB`, the alert says re-grabs
are off. The re-grab covers only the job's own file. Every other file of the download that its quick check flagged, or
could not sort, has a burn-in job of its own. When the second pass disagrees or cannot tell, the alert says the problem
was not confirmed, and nothing changes.

**One re-grab per item for its own language.** Text in the language of the audio, or of the item's original language,
may be in every release, such as Chinese text on a Chinese film. AMG re-grabs such a movie or episode once, and records
that in the `burned_in_once` namespace, keyed by `runner.burn_item()`. The next copy with such a burn-in stays, with an
alert. When the store is too busy to record it, the decision line gets a note, and the next copy is re-grabbed once
more. Text in any other language follows the normal rule above.

**One alert.** The job posts one `burned_in` alert per file and size. A failed flag edit is part of that alert, never a
separate "edit" alert. With `DISCORD_POSTS=all`, no change post repeats the flags the alert names.

**Drops.** Each time the job takes the lock, it checks by the inode that the file is still the one its import checked.
Like a deep analysis, it drops itself when the file is gone or replaced, and follows a move, see "How it runs". A queued
job also drops itself when `BURNED_IN` is `off`, or when the check is not installed.

**Old files.** The check saves no result, so an update never rechecks old files for it. `--burn-in` checks a file by
hand and changes nothing, see [commands.md](commands.md#check-for-burned-in-subtitles).

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
last day. `last_hook_run` is the last write by a job, a backfill, `--sub-time`, `--check --apply`, an audit or the
subtitle hunter.

Each write goes through a temp file and a rename under a lock, with mode 0644. A broken file starts
fresh, and a failed write never stops a job. Give the state directory mode 0751 or wider, so the
agent can open the file by name. No file there holds a secret.

The policy status goes in at the start of each job and of each `--backfill`, `--sub-time`, `--check --apply`,
`--audit` and `arr-media-guard-subhunt` run. A dry run of `--check`, and a dry backfill that queues, write no status.
`--selftest`
records it only with `ARR_MEDIA_GUARD_RECORD=1` in its environment, and it never moves `last_hook_run`. So an install step can clear a policy problem, and a selftest
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
`runner.service_checks()`. With `BURNED_IN` on, the first line says whether the burned-in subtitle check can run, see
"Burned-in subtitles". A tester found that a wrong Plex URL or token stayed silent before. Each
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
