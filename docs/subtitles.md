# The subtitle check

arr-media-guard (AMG) checks each subtitle against what is said in the audio. It finds a subtitle of another episode,
and one that shows its lines early or late. It also finds lines that flash by too fast to read. It needs *language
detection*, the speech model in the Docker image, which on a host is an optional install. On an import the check runs at
the level `SUBTITLES` sets. `--sub-check` runs it over a library, and `--sub-time` on single files. A `.srt` file beside
the video is a *sidecar*, and AMG checks it too. [design.md](design.md#subtitle-match) has the rules behind each check.

## Levels

Set `SUBTITLES` in the env file. The default is `fix`.

| Level | What the check of an import does |
| --- | --- |
| `off` | Nothing. |
| `check` | It checks and alerts, and changes nothing. |
| `fix` | It also takes out a subtitle of another episode, moves such a sidecar aside, gives new times to a subtitle that is early or late, and makes lines that flash stay on screen longer. |
| `deep` | `fix`, and then the deep analysis of the file. |

- The *deep analysis* is a slower subtitle check after an import. It waits in the background queue and runs only while
  no import waits. It also reads the subtitle tracks that the file's index leaves out, and listens to one part of the
  audio in each minute. It times the subtitles the import could not, and moves a stretch of lines that is out of sync,
  see [Subtitle block timing](#subtitle-block-timing). It times live captions line by line, see
  [Live caption timing](#live-caption-timing). It times subtitles in a language no audio track speaks, see
  [Foreign subtitle timing](#foreign-subtitle-timing). It also repairs garbled text, see
  [Garbled subtitle repair](#garbled-subtitle-repair).
- An unknown level acts as `check`, and `--selftest` fails on it.
- `--sub-check` and `--sub-time` do not read `SUBTITLES`. Without `--apply` they report, and with it they fix.

In Docker, a deep analysis that a stopped container left runs after the next start. The listener starts a worker for it
within a minute.

## Check a library

A backfill checks no subtitles unless you add `--sub-check`.

```
arr-media-guard --backfill sonarr --ids 101 --sub-check       # check the subtitles of one series against the audio, dry
arr-media-guard --backfill radarr --sub-check --apply --paths "/data/movies/Film A (2000)/Film A (2000).mkv"
```

`--sub-check` makes the checks of `--sub-time` except [Subtitle block timing](#subtitle-block-timing) and
[Live caption timing](#live-caption-timing), which need much more listening.

- It also takes a file that has only a sidecar to check.
- A dry run prints each result and the change it would make. An apply acts as an import does.
- `--paths` limits the run to the listed files.
- AMG saves the result of each file. A later `--sub-check` skips a file that is unchanged, with the same sidecars, and
  needs nothing more. A fix the check found and did not make, in a dry run or at `SUBTITLES=check`, still counts as
  needed. So a later `--apply` makes it. `--recheck` checks every file again anyway, or only the files of `--ids` or
  `--paths`.
- The result of an import does not count, because an import makes fewer checks. Nor does a result from 2.4.0 or older,
  so the first `--sub-check` after the update checks every file once more.
- A new version of AMG can make some saved results out of date. AMG then checks those files again by itself, see
  [Recheck after an update](features.md#recheck-after-an-update).
- A stopped run goes on where it stopped, because the files it finished are saved.
- It listens at the backfill's low priority, with one thread of the speech model.
- The last line counts the files checked, what it found, the timing fixes and the CPU time. It also estimates the time
  for the whole library.

## Time the subtitles of one file

```
arr-media-guard --sub-time "/data/tv/Show A/Season 1/Show A - S01E02.mkv"           # dry run: changes nothing
arr-media-guard --sub-time "/data/tv/Show A/Season 1/Show A - S01E02.mkv" --apply   # makes the changes
```

`--sub-time` makes every subtitle check on each file it names, as the deep analysis does, and ignores saved results. It
times every subtitle it cannot hear against one whose words matched the audio. It listens to one part of the audio in
each minute. Where a part sits out of sync, it listens to that stretch in full, see
[Subtitle block timing](#subtitle-block-timing).

- The file needs no app. For a path outside Sonarr and Radarr, such as a copy, AMG knows no original language. A
  subtitle that does not match the audio then counts as wrong, unless the file also holds audio in another language.
- With `--apply` and no app, AMG keeps every audio and subtitle flag. Only a subtitle that does not match the audio
  loses its default and forced flags.
- Some old files have subtitle tracks that the file's index leaves out. AMG reads those tracks from the whole file. It
  keeps only the times and the text, and writes no file for it.

### Exit codes

| Code | When |
| --- | --- |
| 0 | All went well. |
| 1 | With `--apply`, an app lookup failed, and the run stops before any change. Or the policy file did not load. |
| 2 | An option was wrong. |
| 3 | With `--apply`, a planned change did not happen. The run lists each such file. |
| 4 | A path holds no file. The run skips it. |

A planned change does not happen when a remux or a header repair failed or was skipped. It also fails when a flag edit
failed, or a sidecar stayed as it was.

### Read the output

Each subtitle gets one line, with these columns.

1. Its place in the file, such as `s3`, or the sidecar's name.
2. The format, the language and the role: `full`, `sdh` or `dub`.
3. How AMG judged it.
   - `words` means AMG compared it with the words heard in the audio. `a reference: in time`, `fixed` or `clean sweep`
     after it means AMG used it to time other subtitles.
   - `reference s1` means AMG timed it against `s1`. `reference, none` means no subtitle could time it.
   - `speech layout` means AMG judged it by where people speak, see [Foreign subtitle timing](#foreign-subtitle-timing)
     and [Incorrect subtitle identification](#incorrect-subtitle-identification).
4. The result. The word check gives `match`, `mismatch` or `unknown`. A subtitle timed against another gives `fit` or
   `weak` with a score, and a weak fit only reports. It can also give `unknown`, or `deferred` when an import ran out of
   time. The speech gives `fit`, `mismatch` or `unknown` with a score.
5. The new times, as a shift in seconds and a frame-rate ratio, or `in time`. `2 lines kept` counts the lines at the
   start or end that keep their times.
6. What AMG did, such as `none`, `retimed`, `removed`, `flags off`, `report only`, `alert only`, or the remux it would
   make.
7. Why.

The lines after them:

- A `flash` line names a subtitle whose lines flash by, and the new end times of its first lines.
- `sweep of s1` starts a table with one row for each part of the audio AMG listened to. A row gives the time and the
  number of words heard, and the share that matched the subtitle. It also gives the lines whose first word matched, and
  how far off they sit. A row marked `ALERT` belongs to a stretch where the subtitle sits 1 second or more off. That
  stretch is large enough to alert. A row that is off but too small to alert says so, such as `one window alone`.
- A line such as `s1: 14 cues from 16:21 to 17:40 moved -1.42 s` names a stretch of lines that AMG moved, see
  [Subtitle block timing](#subtitle-block-timing). A dry run says `would move`. `not moved` means the remux failed, or
  the subtitle is WebVTT, which AMG never rewrites. Its end says whether the moments where speech starts agreed.
- A `no block` line names a stretch AMG listened to and left as it was, with each reason.
- A `garbled` line names a subtitle with garbled characters and where its right text comes from. It gives the language
  it reads as, how many lines were cut short, and what AMG did, see [Garbled subtitle repair](#garbled-subtitle-repair).
- A `live captions` line names live captions timed line by line. It says how many lines moved, and how many stay out of
  sync, see [Live caption timing](#live-caption-timing).
- The last lines give the CPU time of the listening. A `speech read` line gives the time it took to find the speech for
  [Foreign subtitle timing](#foreign-subtitle-timing).
- A line that starts with `ALERT` names a problem, as its Discord alert words it.

The decision log holds the results under `subcheck`, `subtime`, `flash`, `sweep`, `blocks`, `garbled` and `speech`.

### Subtitle block timing

Sometimes only part of a subtitle is out of sync. A release might have a scene cut or added, so the lines before it are
fine and the lines after it show a few seconds early or late. AMG listens to the audio, finds the stretch of lines that
is off, and moves only those lines to where the words are spoken. Lines that are already in sync stay where they are,
and lines never change order. It runs in the deep analysis, and with `--sub-time`. An import never does it.

How AMG finds and moves a stretch:

1. AMG listens to one part of the audio in each minute. A part where the subtitle sits half a second or more off marks a
   stretch to look at. A stretch shorter than about a minute can fall between two parts, and then AMG misses it.
2. AMG listens to that stretch in full, up to 2 minutes past it on each side. So it hears where the lines go back in
   sync. When it still hears no edge, it listens up to 30 seconds further.
3. At least 6 lines in a row must sit off by the same amount, while the lines around them are in sync. A stretch that
   covers over half the file is no stretch. The whole subtitle is off then, and one shift for the whole subtitle fixes
   it.
4. Two separate signs from the audio check the stretch. One is the words AMG hears. The other is the moments where
   speech starts after a pause. When they disagree, nothing moves. When too few such moments are found, the words alone
   decide, and the stretch must sit a full second or more off.
5. A line moves only with its own sign that it belongs to the stretch, such as its own words heard at the new place. A
   line with no sign either way stays. A line whose move would pass a line that stays moves only part of the way. It
   needs two of its own signs for that.

- A moved line may overlap a line next to the stretch. That line keeps its times, and a player shows both lines for that
  moment.
- A subtitle in the file gets the new times in one remux, which AMG checks line by line. A sidecar is written again, and
  AMG keeps the old one. A WebVTT subtitle is never rewritten, so its stretches only report.
- A stretch that moved goes to the decision log only. A stretch that stays out of sync keeps its alert, which names
  where the lines are off. A stretch that `--apply` could not move alerts that the subtitles need new times.

Two kinds of wrong move remain, because nothing in the audio tells them from a real stretch.

- Whisper, the speech model, can hear a whole stretch of a right subtitle early or late. When too few moments of speech
  check it, a stretch heard a full second or more off moves on the words alone. On real right subtitles, the worst such
  stretch measured sat about half a second off.
- A line in sync between two stretches that are off the same way can move with them. That happens when its own words or
  timing put it there.

Listening to a stretch in full costs about 45 CPU seconds a minute of audio. AMG listens to 6 minutes at most for each
audio track, about 4.5 CPU minutes. A file whose subtitles are in sync costs nothing more.

### Live caption timing

Live captions are typed during a broadcast. Each line shows some seconds after it is spoken, late by a different amount
each time. One shift cannot fix that, and neither can a stretch. AMG moves each line of such a subtitle to its own
speech. It runs in the deep analysis and with `--sub-time`.

1. AMG spots live captions in its one-part-a-minute listening. The lines run late, and by amounts that keep changing.
2. AMG listens to the whole audio and finds where the first word of each line is spoken. A speaker's name before a line,
   such as `>> Reporter:`, is never spoken, so it does not count. Neither do the lines of a roll-up caption that repeat
   the line before.
3. Speech comes in the order of the lines, so a first word heard out of that order counts for nothing.
4. A line moves to its first word when it sits a full second or more off it. The 2 lines on each side must sit off the
   same way. So one line that Whisper heard wrong never moves alone.
5. A line whose first word AMG did not hear lies between the lines around it, and moves when they prove where it goes.
6. Every other line stays, and lines never change order. A moved line ends where the next line starts, at most. It still
   shows half a second, or its old length when that was shorter.

When the listening stops part way, the lines past that point stay, and a later `--sub-check` takes the file again. When
a fifth of the lines or fewer stay out of sync, nothing posts. Else one alert says how many lines moved and how many
stay out of sync. At `SUBTITLES=check` it says the setting left them as they are. Listening to a whole track costs about
1 CPU minute a minute of audio. The deep analysis gives way to an import between its parts.

### Foreign subtitle timing

Some subtitles are in a language no audio track speaks, such as English subtitles on a Japanese film. AMG has no words
to compare them with. When no other subtitle in the file can time them, AMG uses the speech instead. It finds where
people speak in the audio track that plays, and fits the subtitle lines to those moments. This finds a subtitle that
shows every line late or early by up to 5 minutes, or one made for another frame rate.

- **Late or early by one amount.** AMG moves only the lines that are off. When the opening is in sync and the rest is
  off, only the rest moves. Song or title lines at the start or end, with no speech under them, keep their times. So a
  subtitle that is off as a whole can keep its first or last line or two where they were.
- **Another frame rate.** A frame-rate error drifts over the whole file, so AMG moves every line. When the lines at the
  start or end clearly sit on speech where they are now, AMG moves nothing and alerts that the subtitles seem late. An
  end that is in sync with no speech under it moves off with the rest. That is a known risk.
- **Checks.** After the move, every part of the subtitle must line up with the speech. The moments where speech starts
  after a pause must confirm the new times in both halves of the file. Else nothing moves, and an alert says the
  subtitles seem late and that no fix lined them up.
- **When it does not run.** A file shorter than 15 minutes gets no new times. Nor does a subtitle read only in part, see
  [Long subtitle track handling](#long-subtitle-track-handling). A WebVTT subtitle never keeps lines at its ends, so
  such a fix only alerts.

It runs in the deep analysis, and with `--sub-check` and `--sub-time`, with `--apply` for the change. It needs language
detection, which also finds the speech. That costs about 40 CPU seconds for a 45-minute episode, and AMG keeps the
result. A subtitle that got new times goes to the decision log only. The rules cannot catch every wrong move, so a line
that was in sync can still move. [design.md](design.md#foreign-subtitle-timing) has the rules and the measurements.

### Incorrect subtitle identification

The same look at the speech can show that a foreign subtitle does not belong to the file. A right subtitle shows its
lines while people speak, and the subtitle of another episode shows them at other times. AMG never changes such a
subtitle. It alerts in two cases.

- The lines do not show while people speak. The alert says they may be from another episode or version.
- The lines fit the speech at different times in different parts of the file. The alert says they may be from another
  version.

A file shorter than 15 minutes gets no verdict. Nor does a subtitle with few lines, or a file with little speech. A
subtitle of sound captions and songs that does not follow the speech gets none either. Live captions that roll on with
no gap get none either. It runs with Foreign subtitle timing, never at an import.
[design.md](design.md#incorrect-subtitle-identification) has the rules and the measurements.

### Garbled subtitle repair

An old muxer could store a subtitle track in the wrong character set. Greek "Καλημέρα, φίλε μου" then shows as
"ÊáëçìÝñá, ößëå ìïõ". Such text is *garbled*. AMG checks each SubRip track for it in the deep analysis and with
`--sub-check` and `--sub-time`, which read the whole file. An import does not. This check needs no language detection.

- AMG reads the track's own bytes in the right character set. When that text reads as the language of the track's tag,
  AMG writes the track back readable. It does that in one remux, which it checks line by line.
- A line the old muxer cut short stays cut, unless a sidecar holds it whole. Such a sidecar, as the `Movie.3.srt` Radarr
  writes, must hold the same lines as the track.
- When more than 2 percent of the lines were cut short, AMG does not repair the track. Some character sets leave no
  trace of a cut, so the count can miss a few.
- A letter that is already right stays, such as the "º" of "40.5ºC", or the "é" of "Café" in Cyrillic text.
- The word check cannot read garbled text, so AMG repairs such a track and does not take it out. The next run checks the
  repaired words and times.
- A track read only in part gets an alert, and no repair.

A garbled track that AMG cannot repair leaves the file, and AMG keeps the old file for `KEEP_ORIGINALS_DAYS`. Its bytes
go beside the video as `<name>.<language>.garbled.txt`, for example `Film (2001).bul.garbled.txt`, and an alert names
it. Players skip that file, and AMG never writes over a file. With `KEEP_ORIGINALS_DAYS` at 0, the track stays, and the
alert says why.

To fix such a track by hand, open the `.garbled.txt` file in a subtitle editor that reads other character sets, such as
Subtitle Edit. Pick the set that makes it readable, save it as a UTF-8 `.srt`, then add it back.

```sh
mkvmerge -o "Film (2001).new.mkv" "Film (2001).mkv" --language 0:bul "Film (2001).bul.srt"
```

### Long subtitle track handling

A styled fansub track, or a picture subtitle redrawn every frame, can hold tens of thousands of lines. AMG reads up to
100,000 lines or pictures of each track. A read stops after 2 minutes for one track, as on a slow network share, or
after 30 minutes when AMG reads the whole file. A track whose read stopped is read only in part.

- No check judges the track past its last line read.
- AMG would have to rewrite every line of the track, and it read only some of them. So the track gets no fix of single
  lines, and no new times from the speech. One shift for the whole track, from the word check or from another subtitle,
  still moves every line.
- It never times another subtitle, because it would do that from its first part alone.

### Apply the changes

With `--apply`, one remux writes every change of a subtitle in the file. That is new times, new end times, a moved
stretch, a repaired text or a removal. AMG checks every line of the new file before it takes the old name. A sidecar is
written again, or moved aside. AMG keeps the old file or sidecar for `KEEP_ORIGINALS_DAYS` (7) days in
`.<NAME>-originals`. With `KEEP_ORIGINALS_DAYS` at 0, AMG still retimes a subtitle in the file, but takes no subtitle
out and changes no sidecar. [regrabs.md](regrabs.md#kept-originals) says where that folder goes and how to undo a
change.

## Try it on one file

`--sub-time` runs on one file with no Sonarr, no Radarr and no settings, in the Docker image.

1. Put the video in a folder.
2. Open a terminal in that folder.
3. Run the dry run. It mounts the folder at `/media`.
4. Read the output. Then run the second command to make the changes.

Linux and macOS:

```
docker run --rm -it -e PUID=$(id -u) -e PGID=$(id -g) -v "$PWD:/media" ghcr.io/samwiseg0/arr-media-guard:latest --sub-time "/media/Episode.mkv"
docker run --rm -it -e PUID=$(id -u) -e PGID=$(id -g) -v "$PWD:/media" ghcr.io/samwiseg0/arr-media-guard:latest --sub-time "/media/Episode.mkv" --apply
```

Windows PowerShell:

```
docker run --rm -it -v "${PWD}:/media" ghcr.io/samwiseg0/arr-media-guard:latest --sub-time "/media/Episode.mkv"
docker run --rm -it -v "${PWD}:/media" ghcr.io/samwiseg0/arr-media-guard:latest --sub-time "/media/Episode.mkv" --apply
```

The dry run prints one line for each subtitle, then the other lines of [Read the output](#read-the-output). It changes
nothing.

- The first line says that no Sonarr or Radarr is set. That is expected.
- A line that starts with `ALERT` names something the check found. It is no error.
- When the check plans a remux, the line says what `--apply` would do. When `--apply` would skip the remux, the line
  says why and what to fix.

No app names the file, so the run keeps every flag. Only a subtitle whose words do not match the audio loses its default
and forced flags. With `--apply`, a remux that AMG checks writes the new times, the new end times or the removal.

The original stays as a hard link at `.arr-media-guard-originals/<UTC time>/Episode.mkv` in the mounted folder, and the
output names that path.

- `PUID:PGID` must be able to write in the mounted folder. When the dry run plans a remux and that folder is not
  writable, it names the folder, the uid and the gid.
- A folder that refuses hard links gets a checked copy instead, see [regrabs.md](regrabs.md#links-and-copies).
- A later `--apply` that keeps an original in the same folder removes the kept originals older than
  `KEEP_ORIGINALS_DAYS`, 7 days by default. Move a kept original out of that folder to keep it longer.

To undo the change, move the kept original back over the file:

```
mv ".arr-media-guard-originals/<UTC time>/Episode.mkv" "Episode.mkv"                        # Linux and macOS
Move-Item -Force ".arr-media-guard-originals\<UTC time>\Episode.mkv" "Episode.mkv"          # Windows PowerShell
```

The exit codes are those of `--sub-time` above.

The one-part-a-minute listening takes a few minutes of CPU. A 24-minute episode took about a minute on the test host. A
stretch that sits out of sync adds more, see [Subtitle block timing](#subtitle-block-timing). The run was tested on
Linux. Windows with Docker Desktop, and Apple Silicon, which runs the amd64 image under emulation, were not tested.
