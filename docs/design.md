# Design

arr-media-guard is a Sonarr and Radarr Custom Script connection. On every import and upgrade it
makes the right audio track play first and sets the subtitle defaults to match. It checks that the
audio plays and the video decodes, and it re-grabs a file that is certainly broken. The same script
backfills the library, scans it for broken files and hunts for releases with English subtitles.
Some releases of English films carry a foreign default audio track, and Plex plays the default.

This file says what each rule does and why it exists. The README covers the install, the env file,
the policy file and the app connection.

- A *job* is one imported file. The hook queues it, and a *worker* runs it.
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

The rules are in `arr_decide.py`. The data they read is in the policy file, so most new cases need
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
shows no activity, so the scan also waits `PLEX_SCAN_AFTER` after the last analyze in its section. The worker reads
the decision log for this, as `--plex-flush` does, so it also waits for a backfill's analyzes. A backfill writes the
decision line of an analyzed file before its `PLEX_PACE` pause. After logrotate, the reader finishes `LOG.1` first.
An analyze waits while a folder scan of its section is pending.

## Alerts

Every alert is a Discord embed to `DISCORD_WEBHOOK`, one per file and problem. The username names
the app and `INSTANCE`. The embed says the problem and what the hook did, and the footer shows the
TMDB state. Red is broken audio, corrupt video, wrong content or a damaged source. Amber is every
other alert. Green is a clean scan summary. Mentions are off, and the secrets are masked. A marker in
`STATE_DIR/alerts/` stops a repeat, and an upgrade to a file of another size alerts again.

| Kind | Fires when |
| --- | --- |
| Language | No main audio track is English, the original language or a language TMDB lists. |
| Runtime | The trusted duration is far off the listed runtime, see "Metadata checks". |
| Duration | The header duration disagrees with the size or the other duration sources. The size check trusts BPS tags only from mkvmerge, because ffmpeg copies stale tags. Else it allows 50 Mbit/s up to 1080p and 150 above. |
| Content | The evidence adds up to a re-grab. The title says "would re-grab" when `REGRAB` does not list `content`. |
| Audio, Video | See "Broken audio" and "Corrupt video". |
| Edit | mkvpropedit failed, or the second probe shows the old flags. |
| Damaged source | The conversion of an import shows a damaged source, see "Damaged source". |
| Repack, Header | A conversion or a header repair failed, and the original stays. |
| Subtitle, Cut | A subtitle runs far past the end. It is not SubRip, or the file may be cut. |
| Subtitle language | A subtitle's text reads as another language than its tag, and nothing else backs a new tag. |
| Policy | The policy file is missing or does not load. |

A fault that the second check did not find again is amber, "not confirmed". A missing or rejected
TMDB key posts one embed a day. A 429 from Discord waits `retry_after` and retries once.

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
subtitle has 4 or more events a minute. A whole-file read may use 220 of the job's 300 seconds. The
hook skips a file too large for that at `READ_RATE`, and a scan reads it later with no time limit.

**The re-grab.** A certain fault skips the flag edit. A download is one unit. When the app has a
grab record, the worker samples every file of the download that is still in the library. It deletes
each broken one, monitors the items again, and then marks the grab failed once. The app searches at
once and would reject the replacement while a broken file is on disk, so the order matters. A delete
goes to the app's recycle bin, and an upgrade first gets its old file back, see "Restore after a bad
upgrade". `units.json` keeps each download for 7 days. `REGRAB_CAP` (30) limits the re-grabs per app
a day, and a season pack counts once. Broken audio, wrong content, a damaged source and corrupt video
share that one count in `regrabs.json`, and 0 turns every re-grab off. Past the cap, or with no grab
record, the worker only alerts. `REGRAB` lists the kinds that re-grab, `audio,video` by default. A
kind it does not list still gets the grab record, the cap and the second check. The worker then logs
`would_regrab` for wrong content, posts "would re-grab" and deletes nothing.

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
stay an amber "Corrupt video, not confirmed". It counts against the same `REGRAB_CAP` as broken audio.
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
- The bin exists where the hook runs. `--selftest` and Test warn when it does not, as in a container
  that does not mount it.
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

`STATE_DIR/kept-replaced.json` holds one record per link. A record names the old path, the kept
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
`--selftest` and Test warn when the app's saved connection to the hook does not send Grab, and when
the hook cannot hard-link a file on a mount. They probe each mount with a small temp file that they
remove. With the copies working, a bin warning says that the copies stand in.

Two bind mounts of one file system share `st_dev`, but a rename between them fails. So the restore
and the bin warning compare the mount tops too. A bin under another mount top counts as another
volume.

## How it runs

The app waits for the hook to exit. The hook answers the Test event at once. On an import or an
upgrade it writes one job file into `STATE_DIR/queue/`, forks a worker if none runs, and exits 0.
`worker.lock` keeps one worker. The worker runs the queue oldest first and checks it once more after
it drops its lock, so no job is lost. A job older than a day is dropped.

`HOOK_WORKERS` (1) sets how many jobs run at a time. Set it to about the cores the host can spare.
With more than 1, the worker claims each job by an atomic rename into `claimed/`, forks one process
per job, and sends every Plex analyze itself, so the idle-section gate holds.

**Time limit.** A job has 300 seconds from the file lock to mkvpropedit. mkvpropedit has no limit,
because a kill mid-write can break the header. The limit raises `arr_meta.OutOfTime`, because a
network handler would swallow a TimeoutError. The metadata checks after the edit get a new 300
seconds. Every error is logged, and the import never sees one.

**The file lock.** Every job, backfill and scan takes `STATE_DIR/lock` and waits an hour at most.
Reads take it shared. An edit, a re-grab and a conversion's swap take it exclusive. Every taker
first passes `lock.gate`, so a waiting edit stops new readers and waits only for the reads in
flight. Linux flock gives a waiting exclusive lock no preference, so without the gate a scan could
keep an edit out. flock cannot upgrade a lock, so a job that takes it exclusive checks the file's
inode, size, mtime and `units.json` entry again, and runs again when one changed. Hearing holds no
lock, because it may wait for the one model.

**One download.** A re-grab deletes the broken files of the whole download. The jobs of a download
run their checks side by side. A job is *settled* when its checks are done and no re-grab can come
from it. Before a job edits or re-grabs, it waits until every older job of the download is settled.
So a download ends the same way as with one worker.

**Deep analysis.** With `SUBTITLES=deep`, an import job queues one deep analysis job for its file in
`STATE_DIR/deep-analysis/`, named by the path, so a newer import of the path replaces it. The worker runs one only when no
import job waits, one at a time per host, and never drops one by age, only when its file is gone. The queue drains with
no new import, because the worker runs until both queues are empty. A deep analysis has no time limit. Its sweep hears
two windows at a time, and between two of them it yields when an import's hearing waits at `lid.turn.gate` or an import
job waits in the queue. It also stops for a waiting import job after the whole-file read, before each read and each fit
of a track, and before the remux. It then goes back to its queue. Its next run finds the words heard so far in the
cache, and the whole-file read in a `.read` file beside the job. With `HOOK_WORKERS=1` an import job so waits at most
for one step: the whole-file read of one film, the read or fit of one track, one pair of windows, or one remux. With
more workers it runs in a job process of its own. A deep analysis decides with the inputs its import stored in the job:
the original language, the release name, the kids flag and the rest, so it asks no app. It never hears the language
again. It keeps every flag the import set, after a remux of its own too. Only a subtitle verdict changes a flag: a
track whose words do not match the audio loses its default and forced flags.

**Crashes and stops.** A job process that dies goes back to the queue, and the third crash drops the
job. SIGTERM starts no new job, kills each child ffmpeg or mkvmerge, and puts the job back. A job
inside mkvpropedit or a re-grab's deletes blocks SIGTERM until they end. Pending Plex analyzes go to
`plex-pending.json` for the next worker. With `KillMode=process` on the app's unit, a stop of the
app never signals the worker. A decision log line is one `O_APPEND` write, so lines never
interleave. When the app renamed a file before its job ran, the worker asks the app for the new path
by the file's id. A 404 or another item drops the job as `file_gone`.

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
`recheck` the plan right after an edit, which the audit reads.

The outcome, reason and Plex codes are stable, so queries and alerts can match on them. The outcome
codes come from `OUTCOMES` in the script. The reason codes come from the `reason()` calls in
`arr_decide.py` and from the script's header and restore steps. An `editing` line with the undo goes
out before each edit, so a crashed edit still has a record. A missing or broken policy file never
stops the hook. It logs `no_policy` and alerts once. A backfill or an audit refuses to start. Rotate
the log weekly with compression, and keep `delaycompress`. A folder scan reads the rest of `LOG.1` after a rotation,
and plain `compress` has already made it `LOG.1.gz`.

Each decision also goes to syslog as one logfmt line with the tag `NAME`. It holds no path, no track
list and no secret. The app key is `arr`, because a log store such as Loki often puts the syslog tag
in its `app` label.

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
arr-media-guard --audit radarr --plan-from /root/radarr-plans.jsonl --post                 # one summary
arr-media-guard --backfill radarr --apply --plan-from /root/radarr-plans.jsonl --canary 20 # a sample
arr-media-guard --audit radarr --since 2026-01-01T10:00 --post                             # the canary's edits
arr-media-guard --backfill radarr --apply --plan-from /root/radarr-plans.jsonl             # the rest
```

`--plan-from` limits an apply to the files the dry run planned a change for. Each file is planned
again before its edit, so a changed file gets the current plan. `--canary N` takes one file of each
class in turn. `--only-undecided` takes the undecided files, so hearing can decide them without a
probe of the whole library.

## Audit

`--audit` never edits. It prints one summary and, with `--post`, sends it as one embed. With
`--plan-from` it groups a dry run's plans by policy path and rule, counts the undecided and dropped
plans, and checks the invariants again. With `--since 24h` or `--since <ISO date>` it reads the
decision log. It expects no further edit in each edit's `recheck`, and it probes an edit logged
without one.

Schedule `arr-media-guard --audit <app> --since 24h --post` each night for each app, with a systemd
timer or cron. It posts only when the day had an edit, an undecided or dropped plan, a conversion or
a file that is not Matroska. The summary carries the day's TMDB status. The audit also removes kept
originals past their age and writes the policy status, see "Status file".

`--check-audio` and `--check-video` scan every file of the app the way the worker checks an import.
A scan never deletes, edits or re-grabs. It checks `SCAN_WORKERS` files at a time, and each worker
pauses 2 seconds before a file. It keeps its place in `STATE_DIR`, so it resumes over days, and
`--restart` starts a new pass. Problems go to a list file and one summary embed. On NFS, idle I/O
priority has no effect on the file server, so the worker count and the pause are the throttles.

## Reverse an edit

Each edit logs `undo`, the complete mkvpropedit command with the old values, before mkvpropedit
runs. The selectors are track UIDs, so they hold when the track order changed. The README shows how
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
see "Subtitle text". A sidecar that ends past the video, or
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
a new name, a `converting` line and an entry in `convert-pending.json` come first. The extras move
into `.<NAME>-convert`, the new file is linked in as `<base>.mkv`, and the original moves to a held name in
`.<NAME>-convert`. The app then takes the new file. Only then are the original and the sidecars deleted. When a
step fails after that, `convert_undo()` reads the app first and never deletes a file the app lists. A
failure removes the temp file last, after the extras are back, and then the empty `.<NAME>-convert`.

**The app takes the new file.** A `ManualImport` command names the item. It carries the old record's
quality, languages, release group and indexer flags, so the app never parses the new name. The file
is in the item's folder, so the app neither moves nor renames it. The hook puts back the scene name
and Radarr's edition, because only an import from a download sets them. The apps score custom
formats on the scene name, else the download path's name, else the file name. So a new name that
would lose a format scoring above 0, or gain one below 0, refuses the conversion. On Radarr the hook
reads the record that `movieFileId` names, because `movie/<id>` shows the old record while it stays.

**The extras.** The rescan that removes the old record would send its extras to the recycle bin. So
the hook reads them from the app's database, because Sonarr 4 has no API for them, and hides them.
After the import it rescans, waits until the old record's extra rows are gone, and moves the extras
back. A second rescan links them to the new file. Metadata files stay in place, because the app
writes them again.

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
refusal with its "Repack failed" alert, and the original stays.

The re-grab uses the steps and safeguards of "Broken audio". They are the grab record, `REGRAB_CAP`,
the download as one unit, the second check and "Restore after a bad upgrade". The second check runs
the read that showed the damage again, from scratch. mkvmerge remuxes into `/dev/null` and must skip
invalid data in the same audio stream, away from the ends, again. ffmpeg reads the original through the
proof's filters, and ffprobe probes it. The proof does not run again, and its refusal stands. The
re-grab judges only the job's own file. Another file of the download shows its damage in its own
conversion, and joins the unit then. A re-grab that deletes the file ends the job. Otherwise the
original stays and gets the import's audio and video checks. The decision line has the outcome
`damaged_source`, the `regrab` code and `repack.damage`. One red "Damaged source" embed says what the
hook did.

Only a hook job re-grabs. A library backfill with `--convert` lists a damaged file in
`convert-<app>.txt`, and it never re-grabs. The re-grab needs `damage` in `REGRAB`, which is off by
default. Without it the hook checks the original again and posts "Damaged source, would re-grab". A
file ffprobe cannot read is skipped and listed, with no "Repack failed" alert.

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
listed runtime, or when no runtime is listed. One amber "File may be cut" embed goes out instead. It
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
`KEEP_ORIGINALS_DAYS` (7). 0 drops it at once. A conversion keeps nothing, except a forced one. The original is
hard-linked to `<folder>/.<NAME>-originals/<UTC time>/<path from the folder>`. A hard link needs no space and never
leaves the path missing. `keep_root()` picks the folder, the same way for `.<NAME>-recycle`:

1. The top of the mount that holds the file, when `.<NAME>-originals` exists there and is writable. Every install
   before 1.7.0 keeps its folder there.
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
Sonarr's Library Import lists a hidden folder at a root folder's top level as unmapped. `STATE_DIR/kept-folders.json`
records each folder the hook keeps a file in. The nightly audit and the worker's daily prune clear those folders too,
and a folder of another name never.

Some file systems refuse a hard link. A keep folder on another file system does, and so do a file with too many links
and some container or Windows mounts. The original is then copied, with its mode and times. The copy needs the file's size
plus 1 GB free, else the fix is skipped and says why. It is written under a hidden name, compared with the original
by size and hash, and only then renamed to the kept name. So the swap waits for a whole copy, and a crash leaves no
file that looks kept. The log and the report of `--sub-time` say "copied" instead of "hard-linked". Any other link
error skips the fix, as before. To undo a repair, move the kept file back and rescan the item.
`NAME` names the hidden folders, `.<NAME>-originals`, `.<NAME>-recycle` and `.<NAME>-convert`. A `NAME` with other
characters than letters, digits, `.`, `_` and `-` fails `--selftest`, and the script uses `arr-media-guard`.
Each keep and the nightly audit remove the time folders older than the setting.

## Subtitle hunter

Some foreign films have no English subtitle track. The hunter replaces such a file with a release
that has a full English subtitle. It handles Radarr only, because an episode needs its own search. A
release that fails must cost a download, never the current file. So the hunter downloads outside
Radarr, checks the file, and keeps a hard link of the old file until the new one is in place.

`--subhunt radarr --ids 101` lists the ranked candidates, and `--apply` downloads, checks and
imports. The hunter needs a Newznab indexer, such as NZBHydra2, and a SABnzbd download client in
Radarr. It reads their keys from Radarr's database, because the API masks them. SABnzbd's download
path must be the path this script sees. Radarr gets that path through Radarr's path map, unchanged
when no pair covers it.

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
current file to a hidden keep name, and the state file records the link first. The import is a
ManualImport in copy mode, because a move can fail on a network share after Radarr deleted the old
file. When the new file landed, the hunter removes the link and the download. Else the link goes
back, the movie is marked `import_failed`, and a red embed asks for a rescan. A keep link that still
exists stops every later run for that movie.

**Give up.** A failed candidate goes into `STATE_DIR/subhunt-radarr.json`, and Radarr's blocklist
stays as it is. When every candidate fails, the movie is marked `no_subbed_release` and keeps its
file. One amber embed names the other option, an external `.srt` from Bazarr or OpenSubtitles.
`--force` hunts again. Before the first apply, check that a hard link works on the media share
(`ln`, then `stat -c %h` prints 2). Run an apply with `setsid nohup`, because a download can take
long. A later Radarr upgrade can replace the hunted file, so run the hunter again then.

## Metadata checks

`arr_meta.py` checks the app's metadata before a language or runtime alert trusts it. It also adds
up the evidence for a wrong-content re-grab. A correct file must never be deleted. The hook runs the
checks after the flag edit.

- **Languages.** English, the app's original and TMDB's original are always right. TMDB's spoken
  languages are right when TMDB's original is not English. For an English original, audio in a
  spoken language counts as unknown.
- **TMDB.** Each answer is cached for 30 days. A failed call pauses TMDB for 10 minutes, so an
  outage costs one timeout per run, and a failure reads as unknown. By default the module reads
  Radarr's bundled TMDB key from `/opt/Radarr/Radarr.Common.dll`. `TMDB_TOKEN` overrides it. The
  `tmdb` code of a record is `found` (TMDB returned the item's record), `no_record`,
  `tmdb_unavailable`, `tmdb_token_missing` or `tmdb_token_rejected`. Records from before 1.3.0 say
  `ok` for `found`, and every reader takes both. The syslog line says `not_asked` when the run asked
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
  video's, or the one NFO in a folder named like the video or the release. A file with no scene name
  gives the title of Sonarr's own file name, and the alert says "the file name's title", because
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
  number then alerts, and the alert asks to check the order. When Sonarr imported the release to other numbers than its tag,
  its scene numbering already maps the release's order, and the title says nothing. The alert names
  the episode in Sonarr's order, and its absolute number for anime. It asks to check the episode
  order, or to import the file to the episodes it names by hand.

**The re-grab rule.** A re-grab needs two points. The wrong language scores one. So does a release
name that names that language, for movies only. A short or long runtime scores one, and so does a
year mismatch. Another TMDB film that the release's title and year find, whose runtime matches the
file, scores one. A release with an edition word never searches, because TMDB lists some cuts as
films of their own. A movie under half its listing scores two on runtime, when a second source and
TMDB agree. A special never does, because its listing may count the broadcast slot. A series never
gets the release-language point, because a docuseries changes language per episode. The episode
title scores `EPISODE_TITLE_POINTS`, 0, so it alerts "Wrong episode" and never re-grabs. A re-grab on
it would raise that constant.

**The switch.** `REGRAB` leaves out `content` by default. Then the hook still checks the grab
record, the cap and a second check. It then logs `would_regrab`, posts "Wrong content, would
re-grab" and deletes nothing. Read those posts for a while before you turn it on. A verdict judges
only files of the job's own item. The second check probes the file again, hears the audio past the
cache, and asks TMDB through an empty cache.

## Audio language detection

`arr_lid.py` hears the spoken language of one audio track. The hook calls it for a main audio track
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
/opt/arr-media-guard-lid/venv/bin/python arr_lid.py --fetch --model-dir /opt/arr-media-guard-lid/models
touch /opt/arr-media-guard-lid/ready        # last
```

## Subtitle text

`text_language()` in `arr_decide.py` names the language of a subtitle text. It needs no model and no
dependency. Each of 18 languages has a list of common dialogue words: English, Spanish, Portuguese,
French, German, Italian, Romanian, Dutch, Afrikaans, Swedish, Danish, Norwegian, Polish, Czech,
Slovak, Turkish, Russian and Ukrainian. A word in one list only is a *telling* word. Its answer is the *read* language.

- The letters give the script first. Greek, Hebrew, Arabic, Persian, Thai, Korean, Japanese, Chinese,
  Hindi, Tamil, Telugu, Georgian and Armenian text gets the language of its script. Latin and Cyrillic
  text goes to the lists. Serbian and Macedonian share most Russian stopwords, so one of their own
  letters (ј, љ, њ, ћ, ђ, џ, ѓ, ќ, ѕ) rules out Russian and Ukrainian.
- The top language needs 90 percent of the telling words and 25 telling words at least. Its whole list
  must also hold 20 percent of all words. A language with no list stays under that share.
- The count runs every 300 letters. It stops at the first verdict, or at 3,000 letters, about 600
  words, so clear text stops early. Text under 300 letters is short. Short text, mixed text and a
  language with no list get no answer. Text whose top language holds under 75 percent is mixed at
  once. Between 75 and 90 percent the count reads on, because a few early words weigh most.

The read language is a signal, never an authority.

- **Sidecars.** A conversion already reads each sidecar. When the text reads as a language other than
  the one its name gives, the sidecar is muxed with the text's language. The forced flag of the name
  then stays only when the text's language is a main audio language, because a player shows a forced
  track in the audio's language by itself. The name wins when the lists cannot name its language.
  `repack.sidecars` logs `read` and the `mismatch`.
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

## Subtitle match

A text subtitle can hold the lines of another episode or another cut, and so can a sidecar. `arr_subsync.py` checks a
text subtitle against the audio. The owner's rule is "rather no subtitle than a wrong one".

**What it checks.** A SubRip, ASS, SSA, WebVTT or MP4 timed-text track in a full, SDH or dub role, a `.srt` sidecar in a
conversion, and a `.srt` sidecar beside a Matroska file. Bazarr writes its downloads there. Its language must be the
language of a main audio track. The check hears the track that plays when it speaks that language, else the first main
track that does. A forced, commentary or picture track, a forced sidecar, a track in another language and a file under
5 minutes are never checked. Nor is Japanese, Chinese or Thai text, which has no spaces between its words, so the
check could never give a verdict. So a file with no such track or sidecar costs nothing. The check runs on imports, and a
backfill runs it only with `--sub-check`. `SUBTITLES` sets what an import does with it: `off` runs no check, `check`
reads, reports and alerts and changes nothing, `fix` (the default) acts, and `deep` adds the deep analysis. An unknown
level acts as `check`. `--sub-check` and `--sub-time` ignore `SUBTITLES`, because a person asked for them. Without
`--apply` they report, and with it they fix. It needs language detection, see "Audio language detection".

**The read.** `subtitle_cues()` reads every cue with its start and end, by the Cues, as `subtitle_read()` does. A cue
ends at its BlockDuration. A sidecar and an MP4 track are read as SubRip text.

**The hearing.** The check picks two windows of 10 seconds, one between 5 and 25 percent of the file and one between
75 and 95 percent. So the line through them covers most of the file, and a drift shows. Each window is the place with
the most cue words, because dense cues mean speech. Song lyrics (♪) do not count, and the test for them runs after the
tags are gone, because a font colour holds a #. `arr_lid.listen()` hears both windows as one clip, with the pinned
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
  applied, with no alert. An import with the deep analysis on queues it, and the deep analysis judges the track. Else
  only the decision log holds the fix. A check after a conversion picks the same windows again.
- `--sub-time` and the deep analysis hear no such windows, because their sweep judges the fix. After the fix, the
  sweep's windows with 3 matched cues must sit at least as close to the fitted line as they sat to the audio before: as
  many within 0.3 seconds, and at a median distance no larger. A sweep that shows the track in time so blocks the fix,
  and so does a sweep that heard under 3 such windows. The track then keeps its times, with no alert, and a clean
  sweep makes it a reference.
- No ratio that fits is two offsets, a different cut, and the times stay. It alerts when the windows differ by 1
  second or more. A smaller step, as a right track can show, goes only to the decision log and the backfill report.
  Two ratios that fit and move the cues apart by more than 0.3 seconds at the file's ends give no fix and alert, a
  plain offset among them. Two windows cannot tell a plain offset from 24/23.976 when their offsets differ by half a
  second.
- After the fix, 80 percent of the matched cues must fall inside their spans.

A rule that every matched cue agrees within 0.3 seconds fails on right tracks. Right tracks start their cues 0.9
seconds early to 1.5 seconds late against the speech, so the rule takes the window medians and 80 percent of the cues.

**Actions.**

- A Matroska track that does not match leaves the file in a remux (`subtitle_mismatch_removed`), and a "Wrong subtitle"
  alert goes out. A track whose times need a fix gets them in the same remux (`subtitle_retimed`), so a file that needs
  both is remuxed once. The remux runs under the exclusive lock, with the temp file in `.<NAME>-convert`. Its work files, the
  extracted text, the proof's reads and the cover attachments, go into a folder under `STATE_DIR`, never the system
  temp dir, which is often a small tmpfs. The trim, the damage read, the conversion and the video windows keep their
  work files there too. A worker that starts removes a work folder that a killed step left over a day ago. ffmpeg
  copies every
  kept stream with `-copyinkf`, and reads a retimed track from a second input of the same file with `-itsoffset` and
  `-itsscale`. A cue that the fix moves before 0 starts at 0, through the `setts` filter. A negative time would make
  ffmpeg move every stream. When the audio has a codec delay, as AAC, Opus, AC-3 or MP3 that ffmpeg wrote into Matroska
  has, that start at 0 moves the later cues of the track by the delay. The proof then refuses the remux, the file stays
  as it was, and the "subtiming" alert says the remux failed. A fix that moves no cue before 0 passes. mkvmerge moved
  AAC lace times by 2 ms in a Matroska to Matroska remux, and the proof refused it, while ffmpeg keeps the times it
  reads.
- mkvpropedit then puts back the Segment UID, and each kept track's UID, BCP 47 tag, name and flags. ffmpeg reads an
  image attachment, such as a cover, as a picture stream and would write it as a video track. So the map leaves it
  out, and mkvpropedit adds it back with its name, type and UID. The new file must
  keep the type, codec and language of each kept track in order, its UID, its BCP 47 tag, the properties in
  `KEEP_PROPS` (its name, flags and codec private data), and the count of attachments and chapters. The proof of
  "Conversion" must pass with the retimed tracks' times as the fix moves them. It also holds each stream to its own
  start time, so a remux that moved every stream fails. The file keeps its owner and mode, and the original is kept as
  in "Kept originals".
- A removal cannot be undone without the kept original. So with `KEEP_ORIGINALS_DAYS` 0, or when the remux or the
  proof fails, the track stays in the file. It gets the role `unmatched` instead, which no policy lists, so it never
  becomes a default. It loses its default and forced flags with mkvpropedit (`subtitle_audio_mismatch`), and the alert
  says why it stayed. A retime of another track in the same file keeps that. `SUBTITLES=check` keeps the times, the
  tracks, the sidecars and the flags, and alerts.
- A sidecar beside a Matroska file that does not match moves into `.<NAME>-originals` as a kept original, with a log line and
  the alert. A sidecar whose times need a fix is written again with new times, as UTF-8, and its original is kept the
  same way. With `KEEP_ORIGINALS_DAYS` 0 the sidecar stays as it is and alerts. Bazarr can download the same wrong file
  again, and the hook does not tell Bazarr. A later check moves it again.
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
seconds one more Whisper run in that hearing. A drift hearing and a longer window at too few cues cost one more
hearing each, only when the check asks for them. A check makes at most four hearings after the first. More threads cost more CPU time than they save in wall time, so the
check runs one thread. A failure or a timeout gives unknown and never stops the import. The check shares the job's
time limit, 90 seconds at most.

**Cache and backfill.** `lid.sqlite` also holds each file's verdicts and the size and mtime of its sidecars, with a mark
when they ask for an action that no apply made yet. A backfill with `--sub-check` skips a file whose verdicts are cached
for its size and mtime and its sidecars, and ask for nothing more. So a stopped run goes on where it stopped, an apply
after a dry run still acts, and a new sidecar from Bazarr is checked. `--sub-check` also takes a file with a sidecar
that the flag backfill leaves out. The decision log holds each result in `subcheck`, the remux in `subremux` and the
sidecar actions in `sidecars`.

**Flash cues.** A release can store right starts with ends a tenth of a second later, so a player flashes each line.
The check reads the CueDuration of each subtitle entry from the Cues, which mkvmerge and ffmpeg write, so it reads no
block for this. A text track or `.srt` sidecar whose median cue shows under 0.5 seconds, in any language and role,
forced too, flashes. Only a cue with a visible character counts, so a cue of tags or of a zero-width space is left
out, and the track needs 20 such cues. Half its short cues must also end over two frames before the next cue starts.
A sign drawn frame by frame, or a karaoke line, runs straight into its next event, so it never flashes. Right text
tracks show their median cue for a second or more, and a flash track for about a seventh of a second. Only a cue under
0.5 seconds gets a new end: the next cue's start less two frames (0.083 s), at most the start plus twice the reading
time, 3 seconds at least and 7 at most. The reading time counts the visible characters, 17 a second. A cue only gets
longer. A built-in track gets its new ends in
the remux of "Actions": mkvextract writes its text, the ends go in, and mkvmerge reads it back, which keeps each packet
and the ASS header byte for byte. The proof then holds each end to the plan. A sidecar is written again, and its
original is kept. A fix goes to the log only (`subtitle_ends_lengthened`), and a failed one alerts. `SUBTITLES=check`
keeps the ends and alerts. A WebVTT track that flashes is reported and never rewritten. Imports, `--sub-check`,
`--sub-time` and the deep analysis run it.

**Reference timing.** A text track in another language than the audio, Japanese, Chinese or Thai text, a PGS or VobSub
track, and an `.srt` sidecar the word check leaves out get their times from a reference. A reference is a track or
sidecar of the same file whose words matched and whose times are in time or fixed, with its fix applied. Forced and
commentary tracks stay out. The fit reads no words. It compares when the cues show, in the manner of alass, so a
translation that splits or merges lines still fits. It tries each ratio at the offset the cue starts point to, and keeps
the one where the cues cover the most of the reference's, each cue counted up to 10 seconds. The score is that overlap
above its mean at ten offsets far from the best, so dense cues do not score by chance. A right pair scores well over
0.3, and another episode's track or another film's scores near 0. Under 0.3 is a weak fit, which only reports. A fit
then pairs cue starts with reference starts in ten slices of the span, each at its own offset, and the rules of
"Timing" judge the slices. Every slice must lie within 0.3 seconds of the fix, and a ratio needs a middle slice on its
line. So a cut, in one step or in several, gives no fix and an alert, and the alert says the track disagrees with its
reference. Five slices fixed more shifted copies of real tracks, but they let a cut in steps pass several times as
often, so the fit keeps ten.

- **Sparse slices.** A slice where the track or the reference starts under 6 cues is left out, such as the credits or
  a song only one side times. Chance pairs of so few starts can outvote the right offset. Each half of the span needs 3
  slices that are not left out, else no fix.
- **The slice search.** A slice searches its offset within 60 seconds of the whole track's offset, so a cut of up to a
  minute shows. A search over the whole 120 seconds let chance put a slice of a right track far off, which read as a
  cut. A peak more than 1 second from the track's offset must hold over 1.5 times the pairs of any other offset, else
  the slice has no clear offset and the times stay. A peak at the track's offset needs no such margin, because a right
  slice often holds a second peak one line away.

The reference is in audio time. A fixed reference moves by its fix. An in-time reference moves by its own measured
offset, which can reach 0.75 seconds. So a target's offset to the audio is its offset to the reference plus the
reference's offset, and a fix comes only when that total is 0.75 seconds or more. The fix moves the target to the
audio. When a second reference also fits the target, the fix must give the same times against it at both ends of the
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
and it posts only its subtitle alerts. `--sub-check` adds the whole-file read and hears no sweep, because a sweep of a
library would take weeks.

- **The whole-file read.** Old mkvmerge versions wrote no cue entry for a subtitle block, and many files in a library
  come from them. For those tracks `--sub-check`, `--sub-time` and the deep analysis read the whole file once, with one
  ffmpeg at nice 19 and idle I/O that writes each track in its own format into its own pipe. The read keeps only the
  cue times and text as they arrive. A PGS track keeps only its PCS segments, because its pictures can take GBs. So
  the read writes no file and needs no free space. `--sub-check` and `--sub-time` read under the shared file lock.
  The deep analysis reads before it takes the lock, since a film takes minutes. The read is kept by the file's size
  and mtime, so a file that changed meanwhile is read again under the lock. ffmpeg reads mkvmerge's WebVTT codec id
  as unknown, so such a track stays unread.
- **The sweep.** `--sub-time` and the deep analysis hear one 10-second window a minute of each track the word check
  reads, at its densest cues. One arr_lid.py process hears them with the model loaded once, two windows a Whisper run,
  and each pair is cached like the other hearings. Each row gives the heard words, the overlap, the cues whose first
  word matched and their offset. A row with 3 such cues and 1 second or more off the fitted line alerts when its
  neighbour among such rows is off 1 second or more the same way. A part of the file that is off holds such windows in
  a row. One window alone goes to the log only, because Whisper can hear a word with the line before it and move one
  window's offset by a second. The sweep never changes a time.
- **A clean sweep.** A match with too few anchors for a fix is a reference when its sweep is clean: in each half of the
  file 3 windows or more heard 8 words and gave an offset, every window that heard 8 words matched at 50 percent or
  more, and every such offset lies under 0.75 seconds.

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

A dry run prints each subtitle alert as `--apply` would act on it. `sub_alerts()` builds the dry texts from the
remux's codes, and `repack_block()` gives the reason of a skip again. A file with another hard link stays as it is,
flags included. A keep folder that is not writable names the folder, the uid and the gid of the run, which `PUID` and
`PGID` set in Docker. When the folder does not exist, creating it is enough. The decision log, an import and `--apply`
keep the texts of `sub_alerts()`.

A research prototype of lapse's peak sigma scored another episode's track almost as high as a right one, so the fit
keeps the lift.

**Centre channel.** Whisper hears the mono mix of all channels. The centre channel alone kept the language verdicts,
but a subtitle check lost its match where the centre channel was quiet and the mix still held the words. The mix
stays.

## Status file

The hook records two checks in `STATE_DIR/status.json`, the TMDB key and the policy file. A
monitoring agent can read the file and alert on a failed check. Zabbix's `vfs.file.contents` item
with JSONPath preprocessing is one way. `arr_status.py` writes the file.

```
{"version": 1, "last_hook_run": 1790000000,
 "checks": {"tmdb": {"status": "ok", "since": 1789990000, "checked": 1790000000, "error": "", "error_time": 0,
                     "last_24h": {"ok": 14, "unavailable": 0, "token_missing": 0, "token_rejected": 0}, "hours": {}},
            "policy": {"status": "ok", "since": 1789000000, "checked": 1790000000, "...": "..."}}}
```

The tmdb status is `ok`, `unavailable`, `token_missing` or `token_rejected`. A file's `found` and
`no_record` both record `ok`, because TMDB answered. The policy status is
`ok` or `failed`. A check never recorded reads `unknown`. `since` is when the status last changed,
and `checked` is the last check. `error` keeps the last error text after a recovery, cut to 200
characters with anything that looks like a token replaced. `last_24h` counts each status over the
last day. `last_hook_run` is the last write by a job, a backfill, an audit or the subtitle hunter.

Each write goes through a temp file and a rename under a lock, with mode 0644. A broken file starts
fresh, and a failed write never stops a job. Give the state directory mode 0751 or wider, so the
agent can open the file by name. No file there holds a secret.

The policy status goes in at the start of each job and of each `--backfill`, `--audit` and
`--subhunt` run. `--selftest` records it only with `ARR_MEDIA_GUARD_RECORD=1` in its environment,
and it never moves `last_hook_run`. So an install step can clear a policy problem, and a selftest
never hides a stopped audit. The TMDB status goes in only after a live TMDB answer. Useful alerts
are these:

- The tmdb status is `token_missing` or `token_rejected` for an hour.
- TMDB stays unavailable for several hours, from the first failure to the last check. A single
  failure before a quiet night then never fires.
- The policy status is `failed`.
- `last_hook_run` is older than 2 days. The nightly audit writes the policy status every day, so a
  stale file means the audit schedule or the script stopped.

## Webhook

A Custom Script connection runs the script inside the app's container. In Docker, that container
then needs Python, ffmpeg and mkvtoolnix. So the image runs `arr-media-guard --serve`, an HTTP
listener in `arr_serve.py`, and each app gets a Webhook connection to it. The listener writes the
same job file as `hook()`, through `queue_job()`, so the worker, the queue and the checks are one
code path.

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
send it with the first request. The listener refuses a body over 1 MiB before it reads it. It never
uses the path in the body. The file record must belong to the item the body names, and its path
must be a plain absolute path that this container sees. An old file must sit in the item's folder,
and its recycle bin copy in the app's recycle bin, because a restore renames the copy back over the
old path. A refusal answers 4xx or 5xx, and the decision log gets a line with source `webhook`. The
app shows the answer in its Test and in its log.

**Paths.** The image must see the media at the apps' paths, or a path map pairs the two. Each
program has its own map, `SONARR_PATH_MAP`, `RADARR_PATH_MAP` or `PLEX_PATH_MAP`. An empty one
takes `PATH_MAP`, so a setup from before the three maps works unchanged. A tester's setup showed
the need. Sonarr saw TV at `/mnt/TV`, Radarr saw films at `/movies`, and Plex saw them at
`/mnt/TV Shows` and `/mnt/Movies`. One map cannot pair those with the container's paths.

Each path that crosses to a program uses that program's map, and every Radarr or Sonarr instance
counts as its app. `arr()` and `arr_write()` map every path in an API answer to the local path, and
every path in a query or a body back to the app's path. The subtitle hunter and the conversion
reach the app through them too. The listener maps the posted file path and the old files of an
upgrade with the app's map. The Plex lookup, the folder scan and `--plex-flush` map the local path
to Plex's path. The lists `--plex-flush` reads hold local paths. The app database reads give
names and relative paths only. The script joins a relative path to a folder from the API, so the
reads need no map. The hook's Custom Script variables need none either, because a Custom Script
runs where the app runs. The subtitle hunter takes SABnzbd's download path as a local path. A pair
matches whole folder names, so `/mnt/TV` never matches `/mnt/TV Shows`, and the longest pair wins.

`--selftest` and the listener start check each map against the app root folders. A root folder
the script does not see warns, with the app's map setting. A root folder that no Plex library
folder holds or sits in warns, with `PLEX_PATH_MAP`. The check starts from the root folders, so a
Plex library that holds none of them, such as music or another host's library, never warns. Both
are warnings only. A deploy may run `--selftest` while a mount or an app is down. The listener must
start before the apps answer, so it runs the check in a thread.

The script reads `config.xml` and the app's database in `RADARR_DIR` or `SONARR_DIR`,
`/var/lib/<app>` by default, so the compose file mounts the app's config folder there, read-only. SQLite reads a live WAL database through that mount from another container. On a
test Radarr, the image read a movie file row that sat only in the WAL.

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
reads the env file and the policy at its start. In the image, tini is PID 1 and passes SIGTERM, as
`docker stop` sends it, to the listener. The listener sends it on to the worker's process group and
waits for it. A job that has not written goes back to the queue, and a flag edit or a re-grab ends
first, see "Crashes and stops".

**Daily jobs.** A native install runs the nightly audit from a systemd timer, and the audit removes
kept originals and grab links older than `KEEP_ORIGINALS_DAYS`. logrotate rotates the decision log.
In the image, the listener runs both once a day at `AUDIT_TIME`, the audit for each app whose API
key reads. A day the container was down at that time runs at the next start.

**One-off containers.** Every other mode runs in a one-off container of the same image, or with
`docker exec` in the service. A one-off container mounts the same `/config`, so its file lock, its
scan state and its decision log are the service's. flock works across containers on one host,
because both open the same file. A live test held the lock in the service for 20 seconds, and a
one-off apply waited for it. tini gives each mode the signals of a terminal. Python as PID 1 ignores a
SIGTERM it has no handler for. Without tini, `--plex-flush` ran on through a `docker stop` to its
end.
