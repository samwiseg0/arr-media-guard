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

A tag changes only when two signals agree and one of them is the heard language. The signals are the
legacy tag, a BCP 47 tag that names another language, the heard language and the title's language.
A main audio track also has the item's original language. TMDB's spoken languages count only for an
`und` track, because TMDB lists English as spoken for many foreign films. The language with the most
signals wins when it has two or more and no other language has as many.

- The hook hears a main audio track that is `und`, whose two tags disagree, or whose title names
  another language. Commentary and description tracks are never heard.
- A Spanish track tagged `eng` in a Spanish film gets `es`, because the heard and the original
  language beat the tag. A title a muxer copied onto an English dub changes nothing, because the
  heard English keeps the tag.
- A subtitle keeps its language, because no language detection reads text.
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
differ between Sonarr and Plex. Plex and the apps must see the media at the same paths. A new import
is often not in Plex yet. The worker looks again 15, 45, 105, 225, 405 and 600 seconds after the
edit. After that the app's own Plex connection adds the item, with its flags already fixed.

**The idle-section gate.** Plex can crash when an analyze reaches an item while a scan of its
section replaces the item's media. So the section must be idle on two checks of `GET /activities`,
`PLEX_QUIET` apart. A scan that names no section yet, and a single-item refresh, count for every
section. A failed check counts as busy. A busy section defers the analyze by `PLEX_BUSY_WAIT`. After
`PLEX_BUSY_CAP` the worker gives up, because Plex's own scan reads the changed file anyway.

**A folder scan.** A renamed file is not in Plex until Plex scans its folder. So a conversion or a
restore under another name queues a partial scan of its one folder. Plex's work after an analyze
shows no activity, so the scan also waits `PLEX_SCAN_AFTER` after the last analyze in its section.
An analyze waits while a folder scan of its section is pending.

## Alerts

Every alert is a Discord embed to `DISCORD_WEBHOOK`, one per file and problem. The username names
the app and `INSTANCE`. The embed says the problem and what the hook did, and the footer shows the
TMDB state. Red is broken audio, corrupt video or wrong content. Amber is every other alert. Green
is a clean scan summary. Mentions are off, and the secrets are masked. A marker in
`STATE_DIR/alerts/` stops a repeat, and an upgrade to a file of another size alerts again.

| Kind | Fires when |
| --- | --- |
| Language | No main audio track is English, the original language or a language TMDB lists. |
| Runtime | The trusted duration is far off the listed runtime, see "Metadata checks". |
| Duration | The header duration disagrees with the size or the other duration sources. The size check trusts BPS tags only from mkvmerge, because ffmpeg copies stale tags. Else it allows 50 Mbit/s up to 1080p and 150 above. |
| Content | The evidence adds up to a re-grab. The title says "would re-grab" while `WRONG_CONTENT_REGRAB` is off. |
| Audio, Video | See "Broken audio" and "Corrupt video". |
| Edit | mkvpropedit failed, or the second probe shows the old flags. |
| Repack, Header | A conversion or a header repair failed, and the original stays. |
| Subtitle, Cut | A subtitle runs far past the end. It is not SubRip, or the file may be cut. |
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
a day, and a season pack counts once. Past the cap, or with no grab record, the worker only alerts.

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
stay an amber "Corrupt video, not confirmed". `VIDEO_REGRAB_CAP` (30) is a separate daily cap, and 0
turns the video re-grab off. Decode each certain file and each doubt of a `--check-video` scan in
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
the log weekly with compression.

Each decision also goes to syslog as one logfmt line with the tag `NAME`. It holds no path, no track
list and no secret. The app key is `arr`, because a log store such as Loki often puts the syslog tag
in its `app` label.

```
arr=radarr source=hook outcome=edited class="English original: audio switched" edits=2 reasons=kids_dub alerts="" tmdb=ok label="Film A" id=65ab6056ca69
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
log calls a conversion a repack. A conversion keeps no original. It proves every stream first, and
the app must list the new file before the original goes.

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
language, forced and hearing-impaired flags from the name. A sidecar that ends past the video, or
has cues out of order, is timed for another cut and stays beside the file. `mkvmerge
--disable-lacing --track-order` writes a hidden temp file with no video extension, so the apps and
Plex never import a partial file. Any mkvmerge warning fails the conversion. ASF and WMV go through
`ffmpeg -c copy`, because mkvmerge cannot read them. A c608 caption track becomes an English SubRip
track, because mkvmerge drops it.

**The proof.** No stream is decoded. ffmpeg reads both files at once with `-c copy -copyts -f
framemd5` and pairs the streams of each kind. A stream must keep its codec, its count of packets
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
The proof also allows a last sample that an MP4 edit list hides. Any other lost packet fails, and
the file keeps its original.

**The swap.** The original must still have the inode, size and mtime it had before the remux. With
a new name, a `converting` line and an entry in `convert-pending.json` come first. The extras move
into `HIDE_DIR`, the new file is linked in as `<base>.mkv`, and the original becomes a hidden held
name. The app then takes the new file. Only then are the original and the sidecars deleted. When a
step fails after that, `convert_undo()` reads the app first and never deletes a file the app lists.

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
`KEEP_ORIGINALS_DAYS` (7). 0 drops it at once. A conversion keeps nothing. The original is
hard-linked to `<mount>/<KEEP_DIR>/<UTC time>/<path from the mount>`, where the mount is the mount
point that holds the file. With the app root folders and the Plex sections below the mount, nothing
scans the folder. A hard link needs no space and never leaves the path missing. When a link is not
possible, the repair is skipped. To undo a repair, move the kept file back and rescan the item.
`KEEP_DIR` and `HIDE_DIR` must be hidden folder names that start with a dot. Any other value takes the
default.
Each keep and the nightly audit remove the time folders older than the setting.

## Subtitle hunter

Some foreign films have no English subtitle track. The hunter replaces such a file with a release
that has a full English subtitle. It handles Radarr only, because an episode needs its own search. A
release that fails must cost a download, never the current file. So the hunter downloads outside
Radarr, checks the file, and keeps a hard link of the old file until the new one is in place.

`--subhunt radarr --ids 101` lists the ranked candidates, and `--apply` downloads, checks and
imports. The hunter needs a Newznab indexer, such as NZBHydra2, and a SABnzbd download client in
Radarr. It reads their keys from Radarr's database, because the API masks them. SABnzbd's download
path must be the same path on the Radarr machine.

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
  `tmdb` code of a record is `ok`, `no_record`, `tmdb_unavailable`, `tmdb_token_missing` or
  `tmdb_token_rejected`. A cached answer never counts as live, so it never hides a dead key.
- **Duration.** A duration is trusted when two sources agree within 30 seconds or 2 percent. The
  sources are the header, the stream durations, the size over the bitrate, and the last video
  packet. A header that no other source confirms gets a duration alert only.
- **Runtime.** A movie is short under 0.6 and long over 1.4 of its listing. An edition word such as
  Extended or Director's Cut widens that to 0.5 and 2.0. A TV movie or a stand-up special gets 0.5
  and 1.8. A multi-cut file is never long. An episode is judged on the short side only, and only
  with a listing of 20 minutes or more.
- **Year.** The check reads the years before SxxEyy or the first quality word. It skips a year that
  belongs to a title and a daily show's air date. A year within one of an item year is ok.

**The re-grab rule.** A re-grab needs two points. The wrong language scores one. So does a release
name that names that language, for movies only. A short or long runtime scores one, and so does a
year mismatch. Another TMDB film that the release's title and year find, whose runtime matches the
file, scores one. A release with an edition word never searches, because TMDB lists some cuts as
films of their own. A movie under half its listing scores two on runtime, when a second source and
TMDB agree. A special never does, because its listing may count the broadcast slot. A series never
gets the release-language point, because a docuseries changes language per episode.

**The switch.** `WRONG_CONTENT_REGRAB` is off by default. While it is off, the hook still checks the
grab record, the cap and a second check. It then logs `would_regrab`, posts "Wrong content, would
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
language, averaging 0.8 or more. Otherwise `lang` is null, and `why` names the rule that failed.

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

The tmdb status is `ok`, `unavailable`, `token_missing` or `token_rejected`. The policy status is
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
