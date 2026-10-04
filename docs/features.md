# What it does

arr-media-guard runs on every import and upgrade in Sonarr and Radarr. On a host it runs as a **Custom Script**
connection. In Docker the apps reach it through a **Webhook** connection instead, see [docker.md](docker.md).

Some release groups ship an English film with a Portuguese, Turkish or Hindi default track. Plex plays the default
track, so the viewer gets the wrong language. The hook fixes the flags in place with `mkvpropedit`. It never
re-encodes.

On an import the hook queues a job and returns at once. A worker process does the checks and the edit.

Every file it looks at gets one JSON line in the decision log. Every edit logs its undo command first, see
[monitoring.md](monitoring.md). [design.md](design.md) explains each rule and why it exists.

## Default tracks

The hook classifies every audio and subtitle track once. Then it picks the audio and the subtitles from the policy
file, see [policy.md](policy.md). When the signals conflict, it abstains and edits nothing.

## Language detection

When a track's language is in doubt, an optional Whisper model hears the audio. A count of common words reads the
language of subtitle text.

- A sidecar whose text is in another language than its name goes in with the text's language. A sidecar with no language
  in its name takes the language of its text.
- A text track with a wrong tag gets a fixed tag, or an alert. When no audio track speaks its language, it also stops
  showing by itself.
- An `und` track takes the language its text reads.

## Subtitle match

Whisper hears two short windows of the audio. The hook compares the words with each text subtitle and `.srt` sidecar
in the audio's language.

- A subtitle of another episode leaves the file in a proven remux, or stays out of a conversion.
- A sidecar of another episode moves to the kept originals, and a program such as Bazarr can download it again.
- A subtitle that runs late by an offset or a frame-rate ratio gets new times.
- A text subtitle whose cues flash for a tenth of a second gets ends a viewer can read.

`--sub-time` also times the subtitles it cannot hear, other languages and PGS or VobSub pictures, against a subtitle
that matched the audio. An import does that too, as its time allows. `SUBTITLES=deep` adds the rest in a deep analysis
while no import waits. See [subtitles.md](subtitles.md).

### Subtitle block timing

Subtitle block timing moves a block of cues that sits off while the rest is in time, as after an edit, to its speech.
A cue whose move would pass a cue that stays moves part of the way. Two pieces of its own evidence must put it in the
block. `--sub-time` and the deep analysis do this, see [subtitles.md](subtitles.md#subtitle-block-timing).

### Live caption timing

A captioner who types along with a live broadcast shows each line some seconds late, by a different time each line.
Live caption timing moves each line of such a track to its speech. `--sub-time` and the deep analysis do this, see
[subtitles.md](subtitles.md#live-caption-timing).

### Incorrect subtitle identification

Some subtitles have no subtitle of the audio's language to time them, such as an English subtitle on a Japanese film.
No word check can read them. Incorrect subtitle identification checks that their lines show while people speak.
`--sub-check`, `--sub-time` and the deep analysis run it.

- A subtitle whose lines do not line up with the speech alerts that it may be from another episode or version. It
  stays in the file.
- A subtitle whose parts line up at different times alerts that it may be from another version, and keeps its times.
- A subtitle that lines up but runs late by one shift or a frame-rate ratio alerts with how late it seems. For now its
  times stay.

See [subtitles.md](subtitles.md#incorrect-subtitle-identification).

### Long subtitle track handling

Long subtitle track handling reads up to 100,000 blocks of each subtitle track, as a typeset fansub track can hold tens
of thousands. A read stops after 2 minutes on a slow share, or after 30 minutes for a read of the whole file. A track
read only in part can still get a fix of its whole timing. It gets no fix that rewrites single lines, and no check
judges it past its last line read. See [subtitles.md](subtitles.md#long-subtitle-track-handling).

## Subtitle character set detection

Subtitle character set detection reads a sidecar in an old character set, such as Big5 or Windows-1250. It tries each
character set in turn and keeps the one whose text reads as a language that set writes. A conversion muxes the sidecar
decoded right, and the subtitle check reads it the same way. A sidecar that no character set reads goes in as
Windows-1252, as before.

## Garbled subtitle repair

An old muxer could garble the characters of a SubRip track. It stored text in another character set as Western
European letters. Garbled subtitle repair gives the track its text back in a proven remux, from its own bytes, when
the repaired text reads as its language. A sidecar of the same lines fills in the lines the muxer cut short. A track
that cannot be repaired leaves the file, and its bytes stay beside the video in a `<name>.<language>.garbled.txt`
file. `--sub-check`, `--sub-time` and the deep analysis do this, see
[subtitles.md](subtitles.md#garbled-subtitle-repair).

## Broken audio and corrupt video

The hook samples the audio that will play and decodes three short video windows. A certain fault deletes the file,
marks the grab failed and lets the app search again. A daily cap limits this, see [regrabs.md](regrabs.md).

## Restore after a bad upgrade

When the broken file was an upgrade, the old file comes back from the app's recycle bin, if it checks clean. Turn on
the app's recycle bin, on the media's file system. `--selftest` and Test warn when it is off or elsewhere. With
`KEEP_REPLACED=true`, the hook keeps its own hard link of each file a grab may replace. Then the restore also works
with no usable recycle bin. See [regrabs.md](regrabs.md).

## Wrong content

The hook checks the file against TMDB and its own duration. It alerts, and it re-grabs only when you turn that on.

For an episode, it also reads the episode title from the release's NFO or name. When that title belongs to another
episode of the series, it alerts "Maybe the wrong episode" and names both episodes. That alert never re-grabs.

## Header repair

A lossless `mkvmerge` remux repairs a Matroska header with a wrong duration or a subtitle that runs past the end. The
original stays for a week.

## Conversion

The hook can convert AVI, MP4, M4V, TS and WebM files into Matroska. It proves every stream of the new file packet by
packet before the original goes. When the conversion of an import shows a damaged source, the hook re-grabs it like
broken audio. See [commands.md](commands.md#convert-files-into-matroska).

## Backfill and scans

The same script fixes the flags of your whole library, scans it for broken audio or corrupt video, and audits its
own edits. See [commands.md](commands.md).

## Subtitle hunter

Some foreign films have no English subtitle track. The command `arr-media-guard-subhunt` searches for a release of
such a Radarr film that has one. It downloads each candidate outside Radarr, through NZBHydra2 and SABnzbd, and checks
it. A download with an English full or SDH subtitle, the listed runtime within 10 percent, audio in the original
language and audio that decodes replaces the old file. A rejected download is deleted, and the old file stays.

```
arr-media-guard-subhunt radarr --ids 123            # rank the releases, dry
arr-media-guard-subhunt radarr --ids 123 --apply    # download, check and import
```

It is dry unless `--apply` is given, and it handles Radarr only. It needs `SABNZBD_API_KEY` and `NEWZNAB_API_KEY`, see
the [settings](../README.md#subtitle-hunter). [design.md](design.md#subtitle-hunter) has the rules.

## Alerts

The hook posts one Discord alert for each problem it leaves unresolved. Set `DISCORD_WEBHOOK` to turn the alerts on.

- **Posts.** A problem posts when its fix failed, a setting turns the fix off, the fix cannot run, or the hook is
  unsure, as in "Audio may be broken" or "Maybe the wrong episode".
- **Log only.** A problem that the hook fixed goes to the decision log and Loki only. That is a re-grab, an old file
  put back that the app picked up again, a repaired file, removed, repaired or retimed subtitles, or a moved subtitle file. The
  other alerts of a file the hook deleted go to the log only too. A "Wrong content" alert names each signal that
  scored. A wrong language or runtime alert beside it goes to the log only when the "Wrong content" alert names that
  signal. The decision log keeps every finding.

An alert bolds the names to look for and its key fact, such as **English subtitles (track 2)** or
**about 2 min 19 s late**. An alert of more than two sentences puts each one on its own line.
