# The subtitle check

arr-media-guard (AMG) checks each subtitle against what is said in the audio. It finds a subtitle of another episode,
and one that shows its lines early or late. It also finds lines that flash by too fast to read. It needs *language
detection*, the speech model in the Docker image, which on a host is an optional install. On an import the check runs at
the level `SUBTITLES` sets. `--sub-check` runs it over a library, and `--sub-time` on single files. A `.srt` file beside
the video is a *sidecar*, and AMG checks it too. [design.md](design.md#subtitle-match) has the rules behind each check.

## Levels

Set `SUBTITLES` in the env file. The default is `deep`.

| Level | What the check of an import does |
| --- | --- |
| `off` | Nothing. |
| `check` | It checks and alerts, and changes nothing. |
| `fix` | It also takes out a subtitle of another episode, moves such a sidecar into `.<NAME>-originals`, gives new times to a subtitle that is early or late, and makes lines that flash stay on screen longer. |
| `deep` | `fix`, and then the deep analysis of the file. |

- The *deep analysis* is a slower subtitle check after an import. It waits in the background queue and runs only while
  no import waits. It also reads the subtitle tracks that the file's seek index does not list. Old mkvmerge versions
  left them out, so only a read of the whole file finds their text. It listens to the whole audio and times every line,
  see [Whole-file timing](#whole-file-timing). It times the subtitles the import could not, including one that drifts
  slowly out of sync, see [Drift timing](#drift-timing). It moves the lines that a jump in the middle of the file put
  out of sync, see [Mid-file jump timing](#mid-file-jump-timing). It moves a stretch of lines that is out of sync, see
  [Subtitle block timing](#subtitle-block-timing). It times live captions line by line, see
  [Live caption timing](#live-caption-timing). It times subtitles in a language no audio track speaks, see
  [Foreign subtitle timing](#foreign-subtitle-timing). It also repairs garbled text, see
  [Garbled subtitle repair](#garbled-subtitle-repair).
- An unknown level acts as `check`, and `--selftest` fails on it.
- `--sub-check` and `--sub-time` do not read `SUBTITLES`. Without `--apply` they report, and with it they fix.

In Docker, a deep analysis that a stopped container left runs after the next start. The listener starts the worker for
it within a minute. The worker is the AMG process that runs the queued jobs.

## Check a library

A backfill checks no subtitles unless you add `--sub-check`.

```
arr-media-guard --backfill sonarr --ids 101 --sub-check       # check the subtitles of one series against the audio, dry
arr-media-guard --backfill radarr --sub-check --apply --paths "/data/movies/Film A (2000)/Film A (2000).mkv"
```

`--ids` takes the app's own ids, never TMDB or TVDB ids, see [commands.md](commands.md#dry-runs-and-the-backfill).

`--sub-check` makes the checks of `--sub-time` except [Drift timing](#drift-timing),
[Subtitle block timing](#subtitle-block-timing) and [Live caption timing](#live-caption-timing), which need much more
listening.

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
times every subtitle whose words it cannot compare with the speech against one whose words matched the audio. It listens
to the whole audio of each subtitle whose words match, and times every line, see [Whole-file timing](#whole-file-timing).

- The file needs no app. For a path outside Sonarr and Radarr, such as a copy, AMG knows no original language. A
  subtitle that does not match the audio then counts as wrong. The exception is a file that also holds audio in a
  language other than the subtitle's, because the subtitle may translate that audio.
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

1. Its place in the file, such as `s3` for the third subtitle track, or the sidecar's name.
2. The format, the language and the role. `full` holds all dialogue. `sdh` holds all dialogue and the sounds, for deaf
   and hard-of-hearing viewers. `dub` is a transcript of an English dub.
3. How AMG judged it.
   - `words` means AMG compared it with the words heard in the audio. `a reference: in time` or `fixed` after it means
     AMG used it to time other subtitles.
   - `reference s1` means AMG timed it against `s1`. `reference, none` means no subtitle could time it.
   - `speech layout` means AMG judged it by where people speak, see [Foreign subtitle timing](#foreign-subtitle-timing)
     and [Incorrect subtitle identification](#incorrect-subtitle-identification).
4. The result. The *word check*, the compare of the subtitle with the heard words, gives `match`, `mismatch` or
   `unknown`. A subtitle timed against another gives `fit` or `weak` with a score, and a weak fit only reports. It can
   also give `unknown`, or `deferred` when an import ran out of time. The speech gives `fit`, `mismatch` or `unknown`
   with a score.
5. The new times, as a shift in seconds and a frame-rate ratio, or `in time`. `2 lines kept` counts the lines at the
   start or end that keep their times.
6. What AMG did, such as `none`, `retimed`, `lines moved`, `removed`, `flags off`, `report only`, `alert only`, or the
   remux it would make.
7. Why.

The lines after them:

- A `flash` line names a subtitle whose lines flash by, and the new end times of its first lines.
- A line such as `s1: whole-file timing, 390 of 400 lines anchored, lag +0.40 s, scatter 0.12 s, 200 lines would move`
  sums up the whole-file timing of a subtitle. An *anchored* line is one whose first spoken word AMG found in the audio.
  The *lag* is how late the anchored lines sit, at their median. The *scatter* is how far they spread around the
  timing AMG fitted, and `(live captions)` after it marks live captions. A dry run says `would move`. `not moved` means
  the remux failed, or the subtitle is WebVTT, which AMG never rewrites.
- A `whole segment` or `fine segment` line names a stretch of lines that sits half a second or more off. It gives the
  first and the last anchored line, the anchors, the frame-rate ratio and how far off it sits. It ends with whether it
  `moves` or `stays`, and the rule that decided. `whole` is the first pass over the file, and `fine` the second, which finds short
  stretches.
- A `still off` line names lines that sit three quarters of a second or more off after the moves, where and by how much.
- A `garbled` line names a subtitle with garbled characters and where its right text comes from. It gives the language
  it reads as, how many lines were cut short, and what AMG did, see [Garbled subtitle repair](#garbled-subtitle-repair).
- The last lines give the CPU time of the listening. A `whole-file hearing` line names the windows AMG heard, their CPU
  time, the windows that came from the cache, and why a step of the listening failed. A `speech read` line gives the
  time it took to find the speech for [Foreign subtitle timing](#foreign-subtitle-timing).
- A line that starts with `ALERT` names a problem, as its Discord alert words it.

The decision log holds the results under `subcheck`, `subtime`, `flash`, `whole`, `whole_facts`, `garbled` and `speech`.

### Roll-up captions

Roll-up captions are closed captions that show each new line under the lines before it. A line shows again in each
caption until it rolls off the top, often two or three times. So the first words of a caption were spoken seconds
before it shows. A check that timed a caption by them read the track as seconds late, and could alert that it was.

AMG times each caption of such a track by its new line, which is spoken as the caption shows. A speaker's name before a
line, such as `>> Reporter:`, does not count. A roll-up track in sync no longer gets a "late" alert. A roll-up track
that is really out of sync still gets its fix, or its alert.

### Whole-file timing

The deep analysis and `--sub-time` listen to the whole audio behind each subtitle whose words match it, and time every
line against it. An import never does this, because it takes minutes.

1. AMG listens to the whole audio track in parts of 10 seconds, a new part every 7.5 seconds, so the parts overlap a
   little. The output calls each part a *window*. AMG keeps each window it heard for the file. So a run that stopped
   goes on where it stopped, and a second run listens to nothing new.
2. AMG lines up the words of every subtitle line with the words it heard, in order, over the whole file. A line is
   *anchored* where its first spoken word was heard. A line of song lyrics, a line shown for a split second, and two
   lines in a row with the same words get no anchor. A speaker's name, such as `>> Reporter:`, does not count, and
   neither do the repeated lines of [roll-up captions](#roll-up-captions).
3. AMG fits one timing through the anchors. It is a chain of stretches. Each stretch sits off the speech by a steady
   amount, or drifts at a frame-rate ratio. A new stretch starts only at a real change, such as a jump in the middle of
   the file. A second pass looks for short stretches inside the first ones.
4. A stretch that sits half a second or more off its speech moves onto it when the evidence is strong enough.
   - A stretch of 30 anchored lines or more moves on the heard words alone.
   - A shorter stretch moves when the moments where speech starts after a pause agree with the move. It never moves
     when they disagree, or when it sits over 10 seconds off.
   - On the heard words alone, a shorter stretch moves with 6 anchored lines a second or more off. It also moves with
     15 anchored lines three quarters of a second or more off. 3 anchored lines 2 seconds or more off move when they
     agree closely, as at a jump near the end of a file.
5. A line with no anchor moves with its stretch when the anchored lines on both sides of it belong to that stretch.
   They must lie within 15 seconds of it, unless the stretch holds 30 anchored lines or more. An anchored line moves
   toward its own speech, and never past it. A line with no anchor moves no farther than the anchored lines next to
   it. Lines never change order. Two lines stay half a
   second apart, or as close as they were. A line that would start before the file's start stays.
6. A moved line keeps how long it shows. It ends two frames before the next line starts, and shows half a second at
   least, unless the next line starts sooner. Lines shown inside a long line do not count as its next line. A line
   that ran up to the next line still does. So a move opens no gap, and puts no two lines on screen at once.

After the moves, AMG fits the timing again where the lines now sit. 10 anchored lines in a row three quarters of a
second or more off post an alert. So do 3 or more anchored lines 2 seconds or more off that agree closely. The alert
says what moved and where lines are still off, see [Timing outcome](design.md#timing-outcome). A subtitle in sync, or
fixed, posts nothing. Under 20 anchored lines AMG judges nothing, and under 10 it moves nothing.

- A subtitle in the file gets the new times in one remux, which AMG checks line by line. A sidecar is written again,
  and AMG keeps the old one. A WebVTT subtitle is never rewritten, so its moves only report.
- When the listening stops part way, as when the speech model fails, AMG moves nothing and judges nothing. An alert
  the import held then posts. The result stays pending, but nothing queues a new run. A later `--sub-time` or deep
  analysis of the file goes on where the listening stopped.
- The listening runs on the CPU. It costs about 15 CPU minutes an hour of video, about 5.5 for a 22-minute episode and
  about 30 for a 2-hour film. The deep analysis gives way to an import between two pairs of windows.
- Known limit: a line with no heard words moves no farther than the heard lines next to it. When one of them stays,
  the line stays too, even when the stretch around it is off.
- Known limit: a stretch of lines with no heard words right before a jump can move with the part after the jump.
- Known limit: the speech model can hear a long stretch late. When too few moments where speech starts check it, AMG
  moves that stretch.
- Known limit: a stretch of 3 to 5 anchored lines off by under 2 seconds, or of 6 to 9 anchored lines off by under 1
  second, posts nothing. It moves only when the moments where speech starts agree.

### Drift timing

A subtitle made for another frame rate drifts. Its lines start in sync, or nearly, and fall further behind or ahead as
the file plays. A DVD subtitle can drift by about a second over an episode. The deep analysis and `--sub-time` read a
drift as one stretch at a frame-rate ratio. They move every line onto its speech, see
[Whole-file timing](#whole-file-timing).

The check of an import hears two or three short parts of the audio. One of them can fall on lines the subtitle's
author timed badly. No single fix then fits them, and the import alerts that the subtitle is out of sync by different
amounts. A part is unsure when its heard words sit in sync and its line starts sit three quarters of a second or more
off, 0.8 seconds or more apart. Lines shown early over a sound before their words do that, and so do the lines of a
real stretch out of sync. An unsure part counts toward the import's alert. With `SUBTITLES=deep` the import holds that
alert, and the deep analysis times every line. When every place the import heard off was heard again, and nothing is
still off there, the alert does not post.

- An import fixes a frame-rate drift when a middle part of the audio confirms it. Its new times must make more lines
  show while their words are heard than the lines do now. A tie keeps the times. On a subtitle in sync, a few lines its
  author timed off in the parts the import heard can look like a drift of about a second, and this rule keeps every
  line where it is.
- Known limit: when this rule keeps the times at an import, the import alerts that the subtitle is out of sync. With
  `SUBTITLES` at `fix` or `check`, no deep analysis listens again, so that alert posts even for a subtitle in sync.
  The same holds when the two parts the import hears at a third and at two thirds of the file refuse a small fix.
- An alert of an import's drift says how far off the subtitle is at the start and at the end.
- A recheck of a result that an import or `--sub-check` saved runs at that depth. It never listens to the whole file,
  so it cannot make the fix of the deep analysis. To time such a file, run `arr-media-guard --sub-time <file> --apply`
  on it, see [Time the subtitles of one file](#time-the-subtitles-of-one-file).

### Mid-file jump timing

A release can add or cut a moment of the video in the middle of a file. From that point on, every line of the subtitle
shows the same amount earlier or later than the lines before it. The output calls that point a *jump*. Here a *part*
is the run of lines between two jumps, or between a jump and the start or end of the file. The whole-file timing
starts a new stretch at each jump. Each part that sits half a second or more off moves onto its speech, by the rules
of [Whole-file timing](#whole-file-timing). A subtitle can hold any number of jumps. It runs in the deep analysis
and with `--sub-time`. An import never does it.

- Lines with no heard words between the last line heard before a jump and the first line heard after it stay where
  they are.
- A jump that moved posts nothing when no lines are still off. Else one alert says what moved and where lines are
  still off. With `DISCORD_POSTS=all` the change post says how many lines moved.
- A subtitle that AMG times against the speech or another subtitle gets no jump fix. When its parts sit half a second
  or more apart, it alerts, see [Foreign subtitle timing](#foreign-subtitle-timing).
- Known limit: a jump under half a second stays.

### Subtitle block timing

Sometimes only part of a subtitle is out of sync. A release might have a scene cut or added, so the lines before it are
fine and the lines after it show a few seconds early or late. The output calls such a stretch a *block*. The second
pass of the whole-file timing finds it. AMG moves only those lines to where the words are spoken, by the rules of
[Whole-file timing](#whole-file-timing). Lines that are already in sync stay where they are, and lines never change
order. It runs in the deep analysis and with `--sub-time`. An import never does it.

- A block of 30 anchored lines or more moves on the heard words alone. A shorter block needs the moments where speech
  starts to agree, or more distance on the heard words alone. A block over 10 seconds off stays, unless it holds 30
  anchored lines. Such lines are more likely lines the audio does not hold, as a recap or another cut.
- A block that moved posts nothing. A block that stays out of sync posts, and the alert names where the lines are off.
  A block that `--apply` could not move alerts that the subtitles need new times.

### Live caption timing

Live captions are typed during a broadcast. Each line shows some seconds after it is spoken, late by a different amount
each time. One shift cannot fix that, and neither can a stretch. AMG moves each line of such a subtitle onto its own
speech. It runs in the deep analysis and with `--sub-time`.

1. The whole-file timing spots live captions by their anchors. When they sit over 0.6 seconds from the fitted timing at
   their median, the lines are late by amounts that keep changing. A delay that grows or shrinks steadily is a drift,
   which one stretch fits.
2. Each anchored line moves onto its own speech.
3. A line with no anchor moves by the smaller move of the anchored lines on each side, when both move the same way.
   Else it stays.
4. Lines never change order, and the rules of [Whole-file timing](#whole-file-timing) set the ends.

When no lines are still off after the moves, nothing posts. Else one alert says how many lines moved and how many
stay out of sync. At `SUBTITLES=check` it says the setting left them as they are.

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
- **A jump in the middle.** When two tenths of the lines or more sit half a second or more apart from the rest, AMG
  moves nothing. It alerts that the subtitles line up with the speech at different times in different parts, and
  gives the least and the most they sit off. A drift that no frame rate explains can alert the same way. So does a
  subtitle that AMG times against another subtitle.
- **When it does not run.** A file shorter than 15 minutes gets no new times. Nor does a subtitle read only in part, see
  [Long subtitle track handling](#long-subtitle-track-handling). AMG never rewrites a WebVTT subtitle line by line. So
  when a fix would keep lines at the start or end, a WebVTT subtitle gets only an alert.

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
"ÊáëçìÝñá, ößëå ìïõ". Such text is *garbled*. AMG checks each SubRip track, the text format of `.srt` files, for it. It
does that in the deep analysis and with `--sub-check` and `--sub-time`, which read the whole file. An import does not.
This check needs no language detection.

- AMG reads the track's own bytes in the right character set. When that text reads as the language of the track's tag,
  AMG writes the track back readable. It does that in one remux, which it checks line by line.
- A line the old muxer cut short stays cut, unless a sidecar holds it whole. Such a sidecar, as the `Movie.3.srt` Radarr
  writes, must hold the same lines as the track.
- When more than 2 percent of the lines were cut short, AMG does not repair the track. Some character sets leave no
  trace of a cut, so the count can miss a few.
- A letter that is already right stays, such as the "º" of "40.5ºC", or the "é" of "Café" in Cyrillic text.
- The word check cannot read garbled text, so AMG repairs such a track and does not take it out. The saved result stays
  pending, so the next `--sub-check` checks the repaired words and times.
- A track read only in part gets an alert, and no repair.

AMG takes a garbled track that it cannot repair out of the file, and keeps the old file for `KEEP_ORIGINALS_DAYS`. Its
bytes go beside the video as `<name>.<language>.garbled.txt`, for example `Film (2001).bul.garbled.txt`, and an alert
names it. Players skip that file, and AMG never writes over a file. With `KEEP_ORIGINALS_DAYS` at 0, the track stays,
and the alert says why.

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

The file as it was before the change stays as a hard link at `.arr-media-guard-originals/<UTC time>/Episode.mkv` in the
mounted folder. The output names that path.

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

Listening to the whole audio takes about 5.5 CPU minutes for a 22-minute episode, see
[Whole-file timing](#whole-file-timing). The run was tested on Linux. Windows with Docker Desktop, and Apple Silicon,
which runs the amd64 image under emulation, were not tested.
