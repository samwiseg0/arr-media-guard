# The subtitle check

The subtitle check compares each subtitle with the words heard in the audio. It needs language detection. On an import
it runs at the level `SUBTITLES` sets. `--sub-check` runs it over a library, and `--sub-time` runs it on single files.
[design.md](design.md#subtitle-match) has the rules behind it.

## Levels

Set `SUBTITLES` in the env file. The default is `fix`.

| Level | What the check of an import does |
| --- | --- |
| `off` | No check. |
| `check` | It reads, reports and alerts, and changes nothing. |
| `fix` | It also removes a wrong track, moves a wrong sidecar, retimes and lengthens flash cues. |
| `deep` | `fix`, then a deep analysis after the import. |

- The deep analysis reads the whole file for unindexed subtitle tracks, and hears one window a minute. It times the
  subtitles that had no reference, and moves a block of cues that sits off, see [Blocks](#blocks). It runs only while
  no import waits.
- An unknown level acts as `check`, and `--selftest` fails on it.
- `--sub-check` and `--sub-time` ignore `SUBTITLES`. Without `--apply` they report, and with it they fix.

In Docker, the worker in the service runs the deep analysis while no import waits. A deep analysis that a stopped
container left in the state store runs after the next start. The listener starts a worker for it within
a minute.

## Check a library

A backfill never runs the subtitle check by default. Add `--sub-check`:

```
arr-media-guard --backfill sonarr --ids 101 --sub-check       # check the subtitles of one series against the audio, dry
arr-media-guard --backfill radarr --sub-check --apply --paths "/data/movies/Film A (2000)/Film A (2000).mkv"
```

- It also takes a file with a `.srt` sidecar beside it.
- A dry run prints each verdict and the removal or timing fix it would make. An apply acts as an import does.
- `--paths` limits the run to the listed files.
- The check caches its verdicts. A later run skips a file that stays as it was, with the same sidecars, and needs
  nothing more.
- A stopped run goes on where it stopped.
- It hears at the backfill's priority with one Whisper thread.
- The last line counts the files checked, the verdicts, the timing fixes and the CPU time. It also estimates the time
  for the whole library.

## Time the subtitles of one file

```
arr-media-guard --sub-time "/data/tv/Show A/Season 1/Show A - S01E02.mkv"           # dry run: changes nothing
arr-media-guard --sub-time "/data/tv/Show A/Season 1/Show A - S01E02.mkv" --apply   # makes the changes
```

`--sub-time` runs the subtitle check of `--sub-check` on each file it names, and reads the cache of neither. It then
times every other subtitle against a track or sidecar whose words matched the audio, and hears one window a minute.
Where those windows sit off, it hears that part in full and moves a block of cues that is off, see [Blocks](#blocks).

- The file needs no app. For a path outside Sonarr and Radarr, such as a copy, it runs with no item, so it knows no
  original language. A mismatch then counts unless the file also holds audio in another language.
- With `--apply` and no item, it keeps every audio and subtitle flag. Only a subtitle that does not match the audio
  loses its default and forced flags.
- A subtitle track that old mkvmerge versions left out of the Cues index is read from the whole file. That read keeps
  only the cue times and text as ffmpeg streams them, so it writes no file.

### Exit codes

| Code | When |
| --- | --- |
| 0 | All went well. |
| 1 | With `--apply`, an app lookup failed. The run stops before any change. |
| 3 | With `--apply`, a planned change did not happen. The run lists each such file. |
| 4 | A path holds no file. The run skips it. |

A planned change can fail to happen. Examples are a remux or a header repair that failed or was skipped, a flag edit
that failed, and a sidecar left as it was.

### Read the output

Each subtitle gets one line. The columns are:

1. The track's place, such as `s3`, or the sidecar's name.
2. The codec, the language and the role (`full`, `sdh` or `dub`).
3. The method. `words` is the check against the heard audio. It shows `a reference: in time`, `fixed` or
   `clean sweep` when it times other tracks. `reference s1` means the track was timed against `s1`.
   `reference, none` means no track could time it.
4. The verdict. The word check gives `match`, `mismatch` or `unknown`. The reference timing gives `fit` or `weak`
   with its score, where a weak fit only reports. It can also give `unknown` or `deferred`.
5. The new times, as an offset and a frame-rate ratio, or `in time`.
6. The action, such as `none`, `retimed`, `removed`, `flags off`, `report only` or the remux it would make.
7. Why.

- A `flash` line names a text track whose cues show for a tenth of a second, and the new ends of its first cues.
- The sweep table has one row per heard window. A row holds the window's time, the heard words, the share that matched
  the cues, the cues whose first word matched, and their offset.
- `ALERT` marks the windows of a step, two neighbouring windows 1 second or more off the fitted line the same way, when
  the steps hold 3 rows or more, or every row that heard 3 cues, and at least a quarter of those rows. A part of the
  file is then off.
  A step that does not alert gives its share, such as "in a step of 2 of the 35 windows that heard 3 cues". One such
  window alone is marked "one window alone". Neither alerts.
- A block line names a block of cues that the dense hearing found off, such as `s1: 14 cues from 16:21 to 17:40 moved
  -1.42 s, 80% of 11 heard cues agree within 0.30 s, speech onsets agree (5 in the block, 7 around it)`. Its end says
  what the speech onsets said, see [Blocks](#blocks). A dry run says `would move`. `not moved` means that the remux
  failed, or that the track is WebVTT, which is never rewritten. The sweep rows of a block that moves show where they
  sit after the move.
- A `no block` line names a part that the dense hearing heard and left as it is, and why. A part with several
  reasons gets one line for each.
- The last lines give the cost of the sweep, and of the dense hearing with the speech onsets.
- The decision log holds the results under `subcheck`, `subtime`, `flash`, `sweep` and `blocks`.

### Blocks

A part of a subtitle can sit off while the rest is in time, for example after an edit. One shift for the whole track
cannot fix that. `--sub-time` and the deep analysis find such a part with the sweep, and fix it in six steps.

1. The sweep finds the part. A window that heard enough words, matched half of them and 2 cues or more, sits 0.5
   seconds or more off the track's lean, where most of its windows sit. One such window is enough, so a short block is
   not missed. Steps 3 and 4 decide every move.
2. The dense hearing hears the part in full, in windows of 10 seconds every 7.5 seconds. The part reaches 2 minutes
   past that window, or to the nearest window that sits on the line, so it holds both edges. When the hearing still
   does not see an edge, a second hearing hears up to 30 seconds past it, only where nothing was heard yet, within
   the cap.
3. A block is a run of 6 heard cues or more that sit 0.5 seconds or more off the line, off the fitted line and off
   the track's lean too. They agree with each other within 0.3 seconds, or within a quarter of the shift when that is
   more. No cue in the run sits nearer the line than the block. Of the 6 nearest heard cues before it, 3 must sit on
   the line, or the middle one of the nearest 3 or of all 6 must. The same holds after it, unless the block reaches
   the first or the last cue. 3 cues in a row off the line the way of the block show a longer stretch off, and that
   edge is not seen. The heard words of each cue near an edge say on which side it lies. A cue that fits both sides,
   or has no heard word, stays where it is. The first cue after 2.5 seconds with no cue is never heard as part of a
   block, because Whisper times the first word after a silence early. The line around the block must sit within 0.3
   seconds of the track's lean, where the sweep windows outside every part sit.
4. The speech onsets check the block, as a second clock beside Whisper. An onset is where speech starts after a
   silence of half a second or more. ffmpeg finds them in the centre channel, or in the mono mix of a track with none.
   The cues around the block give the track's onset lead. The block's cues are then counted at two places, where the
   block puts them and where they sat.
5. Each end of the block must hold for more than one anchor. When the onsets agree, each end moves in to the first
   heard cue that sits in the block and that one more thing puts there. That is an onset, its heard words, or an
   anchor 1 second off beside a neighbour in the block. On Whisper alone, the outermost heard cue at each end stays,
   unless an onset puts it in the block, and so does each next one heard under 1 second off the line. At the edge the
   block moves toward, a cue within the shift of the block's outer cue needs an onset where the block puts it,
   because a cue of the block may sit past a cue in time there. An onset within 0.3 seconds of two cue starts may be
   either cue's, so it times neither. The mark of an edit within the first or last 2 heard cues sets the edge. The
   mark is a gap or an overlap between two cues that the move closes.
6. Every cue that moves needs evidence of its own: its own anchor at the block's offset, or its own heard words in
   the block. An onset counts only with the cue's own heard words. The first cue after a long silence needs both its
   words in the block and an onset where the block puts it, because its words alone read early. A cue that starts
   with a cue that stays, as two lines shown at once, stays too. A cue whose own anchor sits over 0.3 seconds off the
   block moves on its words only with an onset there. A cue whose own evidence puts it on the line splits the block
   there, and each side is judged as a block of its own. A cue with no evidence either way stays where it is, and the
   record of the block counts it as unproved. Each cue that moves takes the block's shift, measured against the cues
   around it. No other cue moves, and a cue whose move would pass a cue that stays, or land on its centisecond, stays
   too. On Whisper alone, a block whose move would pass a cue outside it is not moved, because the order is the only
   check left on Whisper's shift.

The speech onsets of step 4 give one of three outcomes.

- They agree. 20 percent of the block's cues, and 3 at least, have an onset where the block puts them, and twice as
  many as where they sat. Those onsets put the block within 0.15 seconds of Whisper's shift. As many of them lie
  within 0.15 seconds of where the block puts the cues, because stray sounds scatter and the block's own speech does
  not. That many must also be rare by chance, against the same count 7 to 53 seconds away. The block moves when it
  sits 0.5 seconds or more off.
- There are too few. Whisper alone decides, and the block moves only when it sits 1.0 seconds or more off. A failed
  read of the onsets counts as too few.
- They disagree. As many cues have an onset where they sat, or the onsets put the block elsewhere. Nothing moves,
  and the `no block` line gives both counts.

- A part that covers over half the file is no block. The whole track is off then, and the timing fix judges it.
- A part that the dense hearing finds in time was noise in the sweep, so its rows never alert. A row that the dense
  hearing heard under 2 cues in keeps its offset.
- A block move never changes the order of the cues. A moved cue may overlap a cue next to the block. That cue keeps its
  times, and a player shows both lines for that moment.
- A built-in text track gets the new times in the one remux, with its timing fix and its flash ends. The packet proof
  checks each start and end. A sidecar is written again, and its original is kept. A WebVTT track is never rewritten,
  so its blocks only report.
- A block that moves posts nothing. A part with no block keeps the sweep alert. So does a sweep window that crosses
  an edge of a block, or holds a cue that stays. A block that `--apply` could not move alerts that the subtitles need
  new times, as a failed fix does.
- An import never sweeps, so it never moves a block. With `SUBTITLES=deep`, its deep analysis does.

The dense hearing costs about 45 CPU seconds a minute of audio. A file whose sweep finds a part hears 6 minutes of audio
at most for each audio track, for all its subtitles together, about 4.5 CPU minutes. The speech onsets of those parts
add a few CPU seconds, about 10 for the audio of a whole episode. A longer part keeps the stretch
around its windows that fits, and a part with under 10 seconds left is not heard. A file whose sweep sits on the line
pays nothing for it.

### Apply the changes

With `--apply`, a built-in track gets its new times, new ends, the new times of its blocks or its removal in one remux
that the packet proof checks. A sidecar is written again or moved. The hook keeps the original file or sidecar for
`KEEP_ORIGINALS_DAYS` (7) days in `.<NAME>-originals`. [regrabs.md](regrabs.md#kept-originals) says where that folder goes and how to undo
a change.

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

The dry run prints one line for each subtitle, the flash check and the sweep of heard windows. It changes nothing.

- The first line says that no Sonarr or Radarr is set. That is expected.
- A line that starts with `ALERT` names something the check found. It is no error.
- When the check asks for a remux, the line says what `--apply` would do. When `--apply` would skip the remux, the line
  says why and what to fix.

No app names the file, so the run keeps every flag. Only a subtitle whose words do not match the audio loses its
default and forced flags. With `--apply`, a remux that the packet proof checks writes the new times, the new ends or
the removal.

The original stays as a hard link at `.arr-media-guard-originals/<UTC time>/Episode.mkv` in the mounted folder, and
the output names that path.

- The mounted folder must be writable for `PUID:PGID`. When the dry run plans a remux and that folder is not writable,
  it names the folder, the uid and the gid.
- A folder that refuses hard links gets a checked copy instead, see [regrabs.md](regrabs.md#links-and-copies).
- A later `--apply` that keeps an original in the same folder removes the kept originals older than
  `KEEP_ORIGINALS_DAYS`, 7 days by default. Move a kept original out of that folder to keep it longer.

To undo the change, move the kept original back over the file:

```
mv ".arr-media-guard-originals/<UTC time>/Episode.mkv" "Episode.mkv"                        # Linux and macOS
Move-Item -Force ".arr-media-guard-originals\<UTC time>\Episode.mkv" "Episode.mkv"          # Windows PowerShell
```

The exit codes are those of `--sub-time` above.

The sweep hears one window a minute and takes a few minutes of CPU. A 24-minute episode took about a minute on the test
host. A part that sits off adds the dense hearing, see [Blocks](#blocks). The run was tested on Linux. Windows with
Docker Desktop, and Apple Silicon, which runs the amd64 image under emulation, were not tested.
