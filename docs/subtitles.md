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
  subtitles that had no reference. It runs only while no import waits.
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
- `ALERT` marks two neighbouring windows 1 second or more off the fitted line the same way. One such window alone is
  marked "one window alone" and does not alert.
- The decision log holds the results under `subcheck`, `subtime`, `flash` and `sweep`.

### Apply the changes

With `--apply`, a built-in track gets its new times, new ends or its removal in one remux that the packet proof
checks. A sidecar is written again or moved. The hook keeps the original file or sidecar for `KEEP_ORIGINALS_DAYS`
(7) days in `.<NAME>-originals`. [regrabs.md](regrabs.md#kept-originals) says where that folder goes and how to undo
a change.

## Try it on one file

`--sub-time` runs on one file with no Sonarr, no Radarr and no settings, in the Docker image.

1. Put the video in a folder.
2. Open a terminal in that folder.
3. Run the dry run. It mounts the folder at `/media`.
4. Read the output. Then run the second command to make the changes.

Linux and macOS:

```
docker run --rm -it -e PUID=$(id -u) -e PGID=$(id -g) -v "$PWD:/media" ghcr.io/samwiseg0/arr-media-guard:2.0.0 --sub-time "/media/Episode.mkv"
docker run --rm -it -e PUID=$(id -u) -e PGID=$(id -g) -v "$PWD:/media" ghcr.io/samwiseg0/arr-media-guard:2.0.0 --sub-time "/media/Episode.mkv" --apply
```

Windows PowerShell:

```
docker run --rm -it -v "${PWD}:/media" ghcr.io/samwiseg0/arr-media-guard:2.0.0 --sub-time "/media/Episode.mkv"
docker run --rm -it -v "${PWD}:/media" ghcr.io/samwiseg0/arr-media-guard:2.0.0 --sub-time "/media/Episode.mkv" --apply
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
host. The run was tested on Linux. Windows with Docker Desktop, and Apple Silicon, which runs the amd64 image under
emulation, were not tested.
