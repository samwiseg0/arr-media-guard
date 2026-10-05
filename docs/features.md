# What it does

arr-media-guard (AMG) checks every file that Sonarr or Radarr imports or upgrades. In Docker the apps post to its
*listener*, a small web server, through a **Webhook** connection, see [docker.md](docker.md). On a host they run it as a
**Custom Script**. AMG puts each file in its *background queue*, answers the app at once, and checks the file from
there. Imports always go first.

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

## Language detection

A track can have no language tag, or a wrong one. *Language detection* is a speech model, Whisper, that hears an audio
track whose language is in doubt. The Docker image holds it, and on a host it is optional. For subtitles, AMG counts
common words in the text. An untagged subtitle takes the language of its text. An untagged audio track takes the heard
language when the film's original language or TMDB agrees. A default or forced subtitle whose text reads as another
language gets a new tag, when a second sign agrees. Else an alert says its language may be wrong. When no audio speaks
that language, it also stops playing by default. This runs at import and in the backfill.

## Subtitle match

A subtitle can belong to another episode, or show its lines early or late. At import, AMG hears two short parts of the
audio, one early and one late, and more when needed. It compares the words with each text subtitle in the language of
the audio. That covers subtitles in the file and `.srt` files beside it, called *sidecars*. This needs language
detection.

- A track whose words do not match leaves the file in a remux. Such a sidecar moves to the folder where AMG keeps old
  files. When AMG cannot remove a track, it stops the track playing by default and alerts.
- A subtitle early or late by one amount gets new times. So does one made for another frame rate, which drifts.
- A text subtitle whose lines flash by too fast to read gets lines that stay on screen longer.
- A subtitle AMG cannot hear, in another language or made of pictures, is timed against one whose words matched. An
  import does this while its time limit allows.

`SUBTITLES=check` only alerts. `SUBTITLES=deep` adds the *deep analysis*, a slower subtitle check after each import. It
hears much more of the audio. It waits in the background queue, and runs only while no import waits. `--sub-check`
checks a library and `--sub-time` single files, see [subtitles.md](subtitles.md).

### Subtitle block timing

Sometimes only part of a subtitle is out of sync. A release might have a scene cut or added, so the lines before it are
fine and the lines after it show a few seconds early or late. AMG listens to the audio, finds the stretch of lines that
is off, and moves only those lines to where the words are spoken. Lines that are already in sync stay where they are,
and lines never change order.

Two separate signs from the audio check the stretch. One is the words AMG hears. The other is the moments where speech
starts after a pause. When they disagree, nothing moves. When too few such moments are found, the words alone decide,
and the stretch must sit a full second or more off. A line moves only when its own words are heard at the new place. It
runs in the deep analysis, and when you run `--sub-time` on a file, see
[subtitles.md](subtitles.md#subtitle-block-timing).

### Live caption timing

Live captions are typed during a broadcast. Each line shows some seconds after it is spoken, by a different amount each
time. AMG spots such a track by its changing delay. It hears the whole audio and moves each line to where its first word
is spoken. A line it cannot place stays, and lines never change order. It runs in the deep analysis and with
`--sub-time`. When over a fifth of the lines stay out of sync, an alert says how many moved, see
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
sidecar with the same lines fills in lines the old muxer cut short. A track AMG cannot repair leaves the file, and an
alert names `<name>.<language>.garbled.txt`, the file beside the video that holds its bytes. It runs in the deep
analysis and with `--sub-check` and `--sub-time`, see [subtitles.md](subtitles.md#garbled-subtitle-repair).

## Broken audio and corrupt video

At import, AMG decodes three samples of the audio that will play. It checks that the video is not cut short or filled
with empty data, and decodes three short parts of it. A sure fault gets a second check from scratch. When that finds it
too, AMG deletes the file through the app and marks the grab as failed, so the app searches again. This is a *re-grab*.
When AMG is unsure, an alert such as "Audio may be broken" says why. `REGRAB` picks the faults that re-grab, and
`REGRAB_CAP` allows 30 a day for each Sonarr or Radarr. When either stops a re-grab, an alert says so. See
[regrabs.md](regrabs.md#re-grabs).

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
its end. A SubRip subtitle can run past the end of the video. After a video check finds no real damage, AMG repairs the
file in a remux. It trims those lines, or removes a track made for another cut. Other subtitle formats cannot be
trimmed, so an alert names them. It runs at import and in a backfill with `--apply`. `HEADER_REPAIR=false` turns it off.

## Conversion

AMG can turn a file in another container, such as AVI, MP4, M4V, TS or WebM, into Matroska. The streams stay the same,
and the sidecars go into the new file. A subtitle whose words do not match the audio stays out. AMG checks every stream
of the new file, packet by packet, before the original goes. `CONVERT=true` turns it on for imports, and `--convert` for
a backfill. When the conversion of an import shows a damaged source, AMG re-grabs only when `REGRAB` lists `damage`.
Else the alert title ends "re-grab is off". See [commands.md](commands.md#convert-files-into-matroska).

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
as for an import. At `check` it only alerts, and a later `--sub-check --apply` makes the fix. At `fix` or `deep` it
fixes. At `off` it does nothing. It keeps the flags an earlier run set. Its fixes and alerts look like those of an
import. `RECHECK_ON_UPDATE=false` turns it off.

The recheck covers only files with a saved result. A file the subtitle check never saw needs one `--sub-check` run.
`--backfill <instance> --sub-check --recheck` checks files again by hand, whatever their saved result says, see
[commands.md](commands.md#dry-runs-and-the-backfill).

Results saved by 2.4.0 and older record no checks, so they do not count. The first `--sub-check` after the update
checks every file once more, and the update queues no recheck for them.

## Subtitle hunter

Some foreign films have no English subtitle track. `arr-media-guard-subhunt radarr --ids 123` looks for another release
of that Radarr film that has one. It downloads each candidate through SABnzbd, outside Radarr, and checks it. A download
must have an English full or SDH subtitle track and working audio in the original language. Its runtime must be within
10 percent of the listed one. With `--apply`, Radarr then imports it. A rejected download is deleted, and the old file
stays. It needs `SABNZBD_API_KEY` and `NEWZNAB_API_KEY`, see the [settings](../README.md#subtitle-hunter) and
[design.md](design.md#subtitle-hunter).

## Alerts

AMG posts a Discord alert for each problem it leaves unresolved, when `DISCORD_WEBHOOK` is set. A fix that failed, a fix
a setting turned off, and a doubt all post, once for the same file. A problem AMG fixed goes to the decision log and
syslog only. Examples are a re-grab, a removed or repaired subtitle, and new subtitle times. With `DISCORD_POSTS=all`,
AMG also posts each change it makes to a file, unless an alert already says it. Imports and the deep analysis post. The
backfill, `--sub-check` and `--sub-time` never post. A scan or the audit posts one summary when it finds a problem.
`arr-media-guard --test-discord` posts a test message and prints Discord's answer.

## Service checks

When the listener starts, it checks each Sonarr and Radarr, and Plex, Discord, TMDB, SABnzbd and the indexer where the
setup uses them. `--selftest` runs the same checks. Each check asks once and changes nothing. A service that does not
answer, or refuses its key, gets a warning line that names it. AMG keeps running, see
[commands.md](commands.md#service-checks).