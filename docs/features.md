# What it does

arr-media-guard (AMG) checks every file that Sonarr or Radarr imports or upgrades. In Docker the apps post to its
*listener*, a small web server, through a **Webhook** connection, see [docker.md](docker.md). On a host they run it as a
**Custom Script**. Here *the app* is the Sonarr or Radarr that sent the file, and the *item* is its movie or series. AMG
puts each file in its *background queue* and answers the app at once. Its *worker*, the AMG process that takes the
queued jobs, then checks the file. Imports always go first.

Most fixes change only track flags and language tags, in place, with `mkvpropedit`. A repair or a subtitle change writes
the file again with the same video and audio, a *remux*. AMG never re-encodes. It checks the new file against the old
one, stream by stream, and keeps the old one for `KEEP_ORIGINALS_DAYS`, see [regrabs.md](regrabs.md#kept-originals).
Each file gets a line in the *decision log*, the file `LOG` names, with what AMG found and changed. Before a flag edit,
AMG logs the command that undoes it, see [monitoring.md](monitoring.md).

## Default tracks

Some releases set the wrong audio to play first, such as a Portuguese track on an English film. At import, AMG works out
the language and the role of every audio and subtitle track, such as main, commentary or forced. It picks the tracks
that play first by the [policy file](policy.md). It turns default flags on or off, turns forced flags off, and fixes
language tags. When the tracks give mixed signs, it changes no flag and logs the file as undecided.

## Original language flag

Matroska has an *Original language* flag. It marks the tracks in the language the film or show was made in, as against
a dub or a translation. Many releases leave it out, and some set it on a dub. At import, AMG sets the flag on each audio
and subtitle track in the original language. It takes the flag off a track in another language that has it.

- The original language is the one TMDB lists. When Radarr or Sonarr names another one, AMG changes no flag.
- AMG changes the flag only on a track whose language it is sure of. A tag alone is not enough.
  - Audio: the heard language must match the tag. When a subtitle's words matched the track at the import's subtitle
    check, that match proves the language, and AMG hears nothing more. Else AMG hears the track with its language
    detection, up to three 30-second samples, and caches the answer. A track whose tag and title name the same
    language needs no hearing.
  - Subtitles: AMG reads the text, and the language it reads must match the tag. A picture subtitle has no text, so
    its flag stays.
- When language detection is not installed or fails, no flag changes. The decision log says why.
- An untagged track keeps its flag, unless AMG tags it in the same run. A commentary and an audio description keep
  their flags. So does every track of a file TMDB does not know.
- An edit of this flag alone does not ask Plex to read the file again, because Plex does not use the flag.
- The edit is one more flag in the same `mkvpropedit` call, with the same undo line. The decision log shows it with the
  other flag edits. With `DISCORD_POSTS=all` a change post says, for example, "Marked the **Japanese audio (track 2)**
  as the original language." It never posts an alert.
- Imports set it. A backfill or `--sub-check` with `--apply` sets it on the files it checks. A backfill skips a file
  with one audio track, no subtitles and no tag to fix, so such a file keeps its flag. The deep analysis and a recheck
  keep the flags the import set, so files imported before 2.7.0 change only in a backfill.

## Language detection

A track can have no language tag, or a wrong one. *Language detection* is a speech model, Whisper, that hears an audio
track whose language is in doubt. The Docker image holds it, and on a host it is optional. For subtitles, AMG counts
common words in the text. An untagged subtitle takes the language of its text. An untagged audio track takes the heard
language when the item's original language, or a spoken language TMDB lists, agrees. A default or forced subtitle whose
text reads as another language than its tag gets a new tag, when a second sign agrees. Else an alert says its language
may be wrong. When no main audio track speaks the language its text reads as, AMG also turns off its default and forced
flags. This runs at import and in the backfill.

## Subtitle match

A subtitle can belong to another episode, or show its lines early or late. At import, AMG hears two short parts of the
audio, one early and one late, and more when needed. It compares the words with each text subtitle in the language of
the audio. That covers subtitles in the file and `.srt` files beside it, called *sidecars*. This compare is the *word
check*. It needs language detection.

- AMG takes a subtitle track whose words do not match out of the file, in a remux. A sidecar that does not match moves
  into `.<NAME>-originals`, the folder of [kept originals](regrabs.md#kept-originals). When AMG cannot remove a track,
  it stops the track playing by default and alerts.
- A subtitle early or late by one amount gets new times. So does one made for another frame rate, which drifts.
- The *deep analysis*, a slower subtitle check after the import, also fixes a drift. It hears one part of the audio in
  each minute. A few parts heard wrong do not block the fix, see [subtitles.md](subtitles.md#drift-timing).
- Roll-up captions repeat the lines above each new line. AMG times each caption by its new line. So a roll-up track in
  sync no longer gets a false "late" alert, see [subtitles.md](subtitles.md#roll-up-captions).
- A text subtitle whose lines flash by too fast to read gets lines that stay on screen longer.
- A subtitle in another language, or one made of pictures, has no words AMG can compare with the speech. AMG times it
  against one whose words matched. An import does this while its time limit allows.

`SUBTITLES=check` only alerts. `SUBTITLES=deep` adds the deep analysis after each import. It hears much more of the
audio. It waits in the background queue, and runs only while no import waits. `--sub-check` checks a library and
`--sub-time` single files, see [subtitles.md](subtitles.md).

### Subtitle block timing

Sometimes only part of a subtitle is out of sync. A release might have a scene cut or added, so the lines before it are
fine and the lines after it show a few seconds early or late. AMG listens to the audio and finds the stretch of lines
that is off, a *block*. It moves only those lines to where the words are spoken. Lines that are already in sync stay
where they are, and lines never change order.

Two separate signs from the audio check the stretch. One is the words AMG hears. The other is the moments where speech
starts after a pause. When they disagree, nothing moves. When too few such moments are found, the words alone decide,
and the stretch must sit a full second or more off. A line moves only when its own words are heard at the new place. It
runs in the deep analysis, and when you run `--sub-time` on a file, see
[subtitles.md](subtitles.md#subtitle-block-timing).

### Live caption timing

Live captions are typed during a broadcast. Each line shows some seconds after it is spoken, by a different amount each
time. AMG spots such a track by its changing delay. A delay that grows or shrinks steadily is a drift instead. One fix
for the whole track corrects a drift, see [Drift timing](subtitles.md#drift-timing). It hears the whole audio and moves
each line to where its first word is spoken. A line it cannot place stays, and lines never change order. It runs in the
deep analysis and with `--sub-time`. When over a fifth of the lines stay out of sync, an alert says how many moved, see
[subtitles.md](subtitles.md#live-caption-timing).

### Foreign subtitle timing

Some subtitles are in a language no audio track speaks, such as English subtitles on a Japanese film. When no other
subtitle in the file can time them, AMG uses the speech. It finds where people speak in the audio and fits the lines
to that. A subtitle early or late by one amount gets new times. When the opening is in sync and the rest is off, only
the part that is off moves. Song or title lines at the start or end, with no speech under them, keep their times. A
subtitle made for another frame rate drifts further off as the file plays, and AMG moves every one of its lines. When
its start or end clearly sits on speech already, AMG moves nothing and alerts instead. The moments where speech starts
after a pause must confirm the new times in both halves of the file. Else the times stay, and an alert says the
subtitles seem late. It runs in the deep analysis and with `--sub-check` and `--sub-time`, and needs language
detection, see [subtitles.md](subtitles.md#foreign-subtitle-timing).

### Incorrect subtitle identification

The same look at the speech can show that a foreign subtitle does not belong to the file. AMG leaves it as it is and
alerts. When its lines do not show while people speak, it may be from another episode or version. When its lines fit the
speech at different times in different parts, it may be from another version. A short file, sound captions, songs and
live captions with no gaps get no verdict. It runs with foreign subtitle timing.

### Long subtitle track handling

A styled fansub track, or a picture track redrawn every frame, can hold far more lines than the dialogue. AMG reads up
to 100,000 lines or pictures of each track. A read stops after 2 minutes for one track, or after 30 minutes for a read
of the whole file. A track read only in part can still get one fix of its whole timing from the word check or another
subtitle. It gets no fix from the speech and no fix of single lines, and no check judges it past the last line read, see [subtitles.md](subtitles.md#long-subtitle-track-handling).

## Subtitle character set detection

Old sidecars are often saved in an old character set, such as Big5 for Chinese or Windows-1250 for Central Europe. AMG
tries each set and keeps the first one whose text reads as a language that set writes. Else it takes the set of the
language in the file name, when the text looks right in it, or Windows-1252. The subtitle check reads the sidecar that
way, and a conversion puts it into the new file with the right characters. The sidecar file itself stays as it is.

## Garbled subtitle repair

An old muxer could store a subtitle track in the wrong character set, so Greek "Καλημέρα" shows as "ÊáëçìÝñá". When such
*garbled* text reads as the track's language in the right character set, AMG writes it back readable in a remux. A
sidecar with the same lines fills in lines the old muxer cut short. AMG takes a track it cannot repair out of the file.
An alert names `<name>.<language>.garbled.txt`, the file beside the video that holds its bytes. It runs in the deep
analysis and with `--sub-check` and `--sub-time`, see [subtitles.md](subtitles.md#garbled-subtitle-repair).

## Broken audio and corrupt video

At import, AMG decodes three samples of the audio that will play. It checks that the video is not cut short or filled
with empty data, and decodes three short parts of it. A sure fault gets a second check from scratch. When that finds it
too, AMG deletes the file through the app and marks the grab as failed, so the app searches again. This is a *re-grab*.
When AMG is unsure, an alert such as "Audio may be broken" says why. `REGRAB` picks the faults that re-grab, and
`REGRAB_CAP` allows 30 a day by default for each Sonarr or Radarr instance. When either stops a re-grab, an alert says
so. See [regrabs.md](regrabs.md#re-grabs).

## Restore after a bad upgrade

When the broken file was an upgrade, the re-grab puts the old file back from the app's recycle bin, if it checks clean.
Turn the bin on, on the media's file system. `--selftest`, the Test button of the app's connection and the listener's
start check warn when it is off or out of reach. With `KEEP_REPLACED=true`, AMG keeps its own hard link of each file a
grab may replace, so no bin is needed. AMG turns on On Grab in the app's connection for that, at the listener's start
and in the nightly audit. The kept files sit in the hidden folders `.<NAME>-originals` and `.<NAME>-recycle`, each with
a `.plexignore`, so Plex never lists them. See [regrabs.md](regrabs.md#restore-after-a-bad-upgrade).

## Wrong content

A release can hold another film or episode. After the flag edit of an import, AMG checks the file against TMDB and the
app. It compares the audio language, the runtime, and the year and title in the release name. Each sign scores points,
and two points mark the content as wrong. AMG re-grabs it only when `REGRAB` lists `content`. Else an alert "Wrong
content, re-grab is off" names each sign. For an episode, AMG reads the episode title from the release's NFO file or
name. When it belongs to another episode, an alert "Maybe the wrong episode" names both. That alert never re-grabs.

## Header repair

A Matroska file can carry a wrong header, such as a wrong length or a missing seek index. It can also hold junk data at
its end. A SubRip subtitle, the text format of `.srt` files, can run past the end of the video. After a video check
finds no real damage, AMG repairs the file in a remux. It trims those lines, or removes a track made for another cut.
Other subtitle formats cannot be trimmed, so an alert names them. It runs at import and in a backfill with `--apply`.
`HEADER_REPAIR=false` turns it off.

## Conversion

AMG can turn a file in another container, such as AVI, MP4, M4V, TS or WebM, into Matroska. The streams stay the same,
and the sidecars go into the new file. A subtitle whose words do not match the audio stays out. AMG checks every stream
of the new file, packet by packet, before it removes the source file. `CONVERT=true` turns it on for imports, and
`--convert` for a backfill. When the conversion of an import shows a damaged source, AMG re-grabs only when `REGRAB`
lists `damage`. Else the alert title ends "re-grab is off". See [commands.md](commands.md#convert-files-into-matroska).

## Backfill and scans

Commands run the checks over the files you already have, see [commands.md](commands.md). `--backfill` fixes the default
tracks and language tags of a library, and is dry unless you add `--apply`. `--check-audio` and `--check-video` look for
broken audio or corrupt video, and only read. The nightly audit reviews the day's edits, and removes kept files older
than `KEEP_ORIGINALS_DAYS`. No command re-grabs.

## Recheck after an update

A new version of AMG can fix a subtitle problem that an older one only reported. AMG saves the result of each file's
subtitle check, so a later `--sub-check` skips that file. Without a recheck, the file would never get the fix.

Each saved result records which checks ran, their versions and what each found. A new version names the checks it
improved, and which old results each change can fix. At the first start of the new version, AMG finds those files and
queues one recheck for each. The container log and the decision log get one line with the count:

```
arr-media-guard: recheck: this version can improve the subtitle check of 12 files checked before. They wait in the background queue for a recheck.
```

On a host install, the first nightly audit of the new version queues them, and they run after the next import.

The rechecks wait in the background queue, where every job waits. Imports always go first, then the deep analyses, then
the rechecks, one at a time. A recheck repeats the check that saved the result, and never checks deeper. A file that
only an import checked gets the import's subtitle checks again, and no more. `SUBTITLES` decides what a recheck may do,
as for an import. At `check` it only alerts, and a later `--sub-check --apply` makes the fix. AMG posts an alert of
one kind once for a file. So at `check`, a recheck posts nothing when an alert of the same kind posted for the file
before. At `fix` or `deep` it fixes. At `off` it does nothing. It keeps the flags an earlier run set. Its fixes and
alerts look like those of an import. `RECHECK_ON_UPDATE=false` turns it off.

The recheck covers only files with a saved result. A file the subtitle check never saw needs one `--sub-check` run. A
recheck reaches only the results whose findings the new version can fix. A recheck of a result that an import or
`--sub-check` saved runs at that depth. It does not listen to each minute of the audio, so it cannot make the drift fix
of the deep analysis, see [Drift timing](subtitles.md#drift-timing). A file saved with no timing finding, such as a
drift an older version called in sync, needs `arr-media-guard --sub-time <file> --apply`.
`--backfill <instance> --sub-check --recheck` checks files again by hand, whatever their saved result says, see
[commands.md](commands.md#dry-runs-and-the-backfill).

Results saved by 2.4.0 and older record no checks, so they do not count. The first `--sub-check` after the update
checks every file once more, and the update queues no recheck for them.

## Subtitle hunter

Some foreign films have no English subtitle track. `arr-media-guard-subhunt radarr --ids 123`, with 123 the film's
Radarr movie id, looks for another release of that film that has one. It downloads each candidate through SABnzbd,
outside Radarr, and checks it. A download must have an English full or SDH subtitle track and working audio in the
film's original language. Its runtime must be within 10 percent of the listed one. With `--apply`, Radarr then imports
it. A rejected download is deleted, and the old file stays. It needs `SABNZBD_API_KEY` and `NEWZNAB_API_KEY`, see the
[settings](../README.md#subtitle-hunter) and [design.md](design.md#subtitle-hunter).

## Alerts

AMG posts a Discord alert for each problem it leaves unresolved, when `DISCORD_WEBHOOK` is set. A fix that failed, a fix
a setting turned off, and a doubt all post, once for the same file. A problem AMG fixed goes to the decision log and
syslog only. Examples are a re-grab, a removed or repaired subtitle, and new subtitle times. With `DISCORD_POSTS=all`,
AMG also posts each change it makes to a file, unless an alert already says it. Imports, the deep analysis and the
recheck post alerts. A conversion that a stop left unfinished gets an alert too, when the worker starts or a `--convert`
run finds it. The backfill, `--sub-check` and `--sub-time` post nothing else. A scan or the audit posts one summary when
it finds a problem. `arr-media-guard --test-discord` posts a test message and prints Discord's answer.

Each alert shows two short fields side by side, under its text and above the item and file. **Check** names the check
that found the problem. The checks are Import check, Deep analysis, Recheck, Worker start and Command line run. The last
two find a conversion that a stop left unfinished, when the worker starts or in a `--convert` run. **Stopped at** names
the step where AMG's fix stopped. The steps are the same for every kind of alert. A subtitle shows one step, the
furthest its fix reached.

| Stopped at | What it means |
| --- | --- |
| Converting to MKV | The conversion of the file to MKV failed or stopped part way. |
| Hearing the speech | AMG could not hear enough of the speech to time the subtitles. |
| Finding the shift | AMG heard the speech, but no one time shift fits the subtitles, as when parts of the file are off by different amounts. |
| Testing the fix | AMG worked out new times or new text, but the result did not pass its check, so the subtitles stay as they were. |
| Writing the file | The fix was ready, but writing it into the file failed or was skipped. AMG skips it when the file is larger than `REPACK_MAX_GB`, or the disk has less free space than twice the file's size. It also skips a file with a second hard link, such as the download client's copy. |
| Replacing the file | A re-grab of the broken file, putting the old file back, or the app taking a converted file in place of the original did not finish. |

Alerts name a track by its language and its role, as a player lists it: "the English SDH subtitles (track 3)". The roles
are SDH, forced, dub, commentary and audio description. An alert adds the track's own title in quotes only when the
title adds something, cut at about 40 characters. A title that only repeats the language, the codec or the role is not
shown. AMG never changes a track's title in the file.

An alert where AMG tried no fix shows only Check. That is a doubt, a check that only reports, or a fix that a setting or
the daily re-grab limit turned off. A change post, the audit post and a scan summary have neither field. The alert of a
TMDB key that failed has neither, and neither does the alert that the state store was broken and moved aside. Nor do the
subtitle hunter's posts.

With `SUBTITLES=deep`, each problem gets one alert. The import holds back its subtitle alerts, because the deep analysis
checks those subtitles again. The deep analysis then posts once, and only what it still finds wrong. A subtitle problem
it fixes gets no alert. With `DISCORD_POSTS=all` it gets the one post of the change. A held alert still posts when
nothing judged its subtitles again. That happens after an error, when AMG could not hear the audio, or when `SUBTITLES`
is `off` by then. It shows the Check and Stopped at of the import. With `HOOK_WORKERS` at 2 or more, it also posts when
the deep analysis crashes three times. With `HOOK_WORKERS=1`, a deep analysis that crashes the worker runs again at the
next start, and its held alert waits. A held alert waits with the deep analysis job in the background queue, so a
restart keeps it. When the app renames or moves the file, the deep analysis asks the app where it is and checks it
there. A held alert goes unposted only when the app replaced the file, or the app says it no longer has the file for
that item. When the app lists the file where AMG cannot see it, as while a mount is down, the held alert posts. With
`DISCORD_POSTS=all`, each change the import made posts once, such as a removed track or a flag it turned off. A held
alert that posts names the change itself. Otherwise the change posts on its own. Every other alert of the import posts
at once, such as wrong content, broken audio or video, and a failed flag change.

## Service checks

When the listener starts, it checks each Sonarr and Radarr, and Plex, Discord, TMDB, SABnzbd and the indexer where the
setup uses them. `--selftest` runs the same checks. Each check asks once and changes nothing. A service that does not
answer, or refuses its key, gets a warning line that names it. AMG keeps running, see
[commands.md](commands.md#service-checks).