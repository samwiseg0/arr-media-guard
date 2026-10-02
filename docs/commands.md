# Commands

The hook works on imports by itself. These commands work on the whole library or on single files. Each one is dry
unless you add `--apply`. `arr-media-guard --help` lists them all.

In Docker, run each command in the image, see [Commands in Docker](#commands-in-docker).

## Connect to the apps

A command that names an app reads it through its API, with the settings `RADARR_URL` and `SONARR_URL`. A command
takes the name of an instance of `APP_INSTANCES` too, as `--backfill sonarr-4k`, and reads that instance's keys.

- The script reads each app's API key from `config.xml` in `RADARR_DIR` or `SONARR_DIR`. For an app whose folder this
  host does not see, set `RADARR_API_KEY` or `SONARR_API_KEY`.
- No command reads an app's database. The API key alone serves every command.
- An app counts as set up when its API key reads, or when its URL differs from the URLs the env files ship.
- An app at a shipped URL with no API key counts as not set up, also when you keep that URL on purpose.
- `--sub-time` says nothing of an app that is not set up. A command that names the app stops with one line.

## Dry runs and the backfill

A backfill fixes the flags of the files already in your library. It runs at nice 19 and idle I/O priority. It takes
the hook's file lock, and it never posts to Discord.

Run a large backfill in steps, and note the time before each apply:

```
arr-media-guard --backfill radarr --plan-out /root/radarr-plans.jsonl                        # dry run, one plan per line
arr-media-guard --audit radarr --plan-from /root/radarr-plans.jsonl                          # the plans, grouped by rule
arr-media-guard --backfill radarr --apply --plan-from /root/radarr-plans.jsonl --canary 20   # 20 files across the classes
arr-media-guard --audit radarr --since 2026-01-01T10:00                                      # check the canary's edits
arr-media-guard --backfill radarr --apply --plan-from /root/radarr-plans.jsonl               # the rest
```

Other useful commands:

```
arr-media-guard --backfill sonarr --ids 101 102               # only these series (movie ids for radarr)
arr-media-guard --backfill radarr --apply --limit 5           # edit five files, then stop
arr-media-guard --backfill radarr --paths "/data/movies/Film A (2000)/Film A (2000).mkv"   # one file, dry
arr-media-guard --backfill sonarr --check-audio --limit 2000  # scan for broken audio, resumes where it stopped
arr-media-guard --backfill sonarr --check-video --restart     # scan for corrupt video, a new pass
arr-media-guard --backfill sonarr --convert                   # the files that are not .mkv, dry
arr-media-guard --backfill sonarr --convert --apply           # convert them, CONVERT_MAX_FILES a run
arr-media-guard --backfill sonarr --ids 101 --sub-check       # check the subtitles of one series against the audio, dry
arr-media-guard --sub-time "/data/movies/Film A (2000)/Film A (2000).mkv"   # time every subtitle of one file, dry
arr-media-guard-subhunt radarr --ids 123                      # find a release with English subtitles, dry. Needs SABnzbd and NZBHydra2.
arr-media-guard --audit radarr --since 24h --post             # the hook's edits of the last day, to Discord
```

- `--paths` limits the flag backfill, `--convert` and `--sub-check` to the listed files. A scan refuses it.
- `--sub-check` and `--sub-time` are the subtitle check, see [subtitles.md](subtitles.md).
- `SCAN_WORKERS` sets the files a dry-run backfill reads at a time. `--workers N` wins.

## Plex after an apply

An apply asks Plex to analyze each edited item, after the section is idle on two checks. When "Scan my library
automatically" is off in Plex, the analyzes that follow need one idle check each. See "The burst" in
[design.md](design.md#plex).

`--plex-later` on a `--convert --apply` run lists the converted folders and scans none of them. `--plex-flush` then
runs one partial Plex scan per library location those folders sit in, instead of one per folder.

## Scans

`--check-audio` scans the library for broken audio, and `--check-video` for corrupt video. Scans never delete or
re-grab. They list what they find in `STATE_DIR`. A scan reads `SCAN_WORKERS` files at a time.

## Convert files into Matroska

`--convert` takes the files that are not `.mkv` and converts them into Matroska. With `CONVERT=true` the hook also
converts every imported file that is not Matroska. A backfill converts only with `--convert`. `CONVERT_MAX_FILES`,
`CONVERT_WORKERS` and `REPACK_MAX_GB` set the limits, see [the settings](../README.md#repairs-and-conversion).

### Force a conversion

The proof refuses a file that it cannot prove lossless, and the decision log holds the refusal. When you checked the
refusal and accept it, name the file:

```
arr-media-guard --backfill radarr --convert --apply --force-convert "/data/movies/Film A (2000)/Film A (2000).mp4"
```

- The run then takes only the listed files.
- A file converts when the proof refuses it for the same reason as its last logged refusal. The state store keeps
  the decision lines of 14 days for this. Another refusal, or no logged refusal, is not forced, and the run says why.
- Every other step runs as normal.
- The original stays in `.<NAME>-originals` for `KEEP_ORIGINALS_DAYS`, so one move puts it back.
- The decision line names the refusal in `repack.forced`, and the nightly audit says the conversion was forced.
- The option needs `--convert --apply` and `KEEP_ORIGINALS_DAYS` above 0.
- A path that is not in the run's work list is reported and skipped.

A listed Sonarr file also skips the check that Sonarr reads its new video name as its own episodes. Scene numbering or
an alias can make that check wrong while the names are right. The conversion names the episodes by id, so the link
stays right. This needs no logged refusal.

- The extras beside the video keep the check. An extra whose name maps to other episodes still refuses the file and is
  named, so you can fix or remove it.
- The proof still runs, and a proof refusal is forced only as above.
- The decision line names the parse result in `repack.forced_name`, the original stays in `.<NAME>-originals`, and the
  audit says "name forced".

## The audit

`--audit` reviews edits. With `--plan-from` it groups a dry run's plans by rule. With `--since` it reads the hook's
edits from the state store, which keeps the decision lines of 14 days. `--post` sends the summary to Discord.

Schedule the nightly audit of each app on a host install, with a systemd timer or cron:

```
arr-media-guard --audit radarr --since 24h --post
arr-media-guard --audit sonarr --since 24h --post
```

Add one line for each instance of `APP_INSTANCES`.

The nightly audit also removes the kept originals and grab links older than `KEEP_ORIGINALS_DAYS`. In Docker,
`AUDIT_TIME` runs it, see [docker.md](docker.md#the-listener).

## Commands in Docker

Every command of a host install runs in the image, as a one-off container or with `docker exec`.

- Both run as `PUID:PGID`. The exit code of the command is the exit code of `docker run` and `docker exec`.
- A one-off container mounts the service's `/config` and its volume `amg-state`. So it takes the same file lock and
  shares the state store.
- Run a long command, such as a backfill, a conversion or a scan, as a one-off container. A restart of the service, as
  an image update does, ends every `docker exec` process in it.
- `docker compose run --rm arr-media-guard ARGS` gives the same one-off container as `$RUN ARGS` below, with the mounts
  of the compose file.

```
RUN="docker run --rm --network media_default -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -v $PWD/arr-media-guard:/config -v amg-state:/config/state -v /srv/media:/data \
  ghcr.io/samwiseg0/arr-media-guard:2.0.0"
EXEC="docker exec -it arr-media-guard arr-media-guard"
```

`media_default` is the compose network, named after the folder of the compose file. Keep `-it` on `docker exec`, so
Ctrl+C reaches the command, and leave it out in a script.

A one-off container writes its logfmt lines to syslog only, and the image runs no syslog. To keep them, as for Loki,
add `-v /dev/log:/dev/log` to `$RUN`. They then go to the host's syslog. The `/dev/log` line of the compose file does the
same for `docker exec`.

Each command needs `/config`. The last column of the table names what else it needs:

- **API**: the app's URL and `<APP>_API_KEY`.
- **Media**: the media at the apps' paths, or a path map. An apply writes there.
- **LID**: language detection, which the image holds.
- **Plex** and **Discord**: `PLEX_URL` and `PLEX_TOKEN`, and `DISCORD_WEBHOOK`. Without them the command skips the
  analyze or the post.

| Host command | One-off container | `docker exec` | Needs |
| --- | --- | --- | --- |
| `arr-media-guard` | none, the Webhook listener takes its place | none | |
| `arr-media-guard --serve` | the service itself | none | API, Media, LID, Plex, Discord |
| `arr-media-guard --selftest` | `$RUN --selftest` | `$EXEC --selftest` | |
| `arr-media-guard --backfill sonarr [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--plan-out FILE] [--workers N]` | `$RUN --backfill sonarr ...` | `$EXEC --backfill sonarr ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --sub-check [--apply] [--ids ID ...] [--paths PATH ...]` | `$RUN --backfill sonarr --sub-check ...` | `$EXEC --backfill sonarr --sub-check ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --apply --plan-from FILE [--canary N]` | `$RUN --backfill sonarr --apply --plan-from /config/FILE ...` | `$EXEC --backfill sonarr --apply --plan-from /config/FILE ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --plan-from FILE --only-undecided [--apply] [--plan-out FILE]` | `$RUN --backfill sonarr --plan-from /config/FILE --only-undecided ...` | `$EXEC --backfill sonarr --plan-from /config/FILE --only-undecided ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --convert [--apply] [--plan-from FILE] [--canary N] [--workers N] [--plex-later]` | `$RUN --backfill sonarr --convert ...` | `$EXEC --backfill sonarr --convert ...` | API, Media, Plex |
| `arr-media-guard --backfill sonarr --convert --apply --force-convert PATH ...` | `$RUN --backfill sonarr --convert --apply --force-convert PATH ...` | `$EXEC --backfill sonarr --convert --apply --force-convert PATH ...` | API, Media, Plex |
| `arr-media-guard --plex-flush sonarr` | `$RUN --plex-flush sonarr` | `$EXEC --plex-flush sonarr` | Plex |
| `arr-media-guard --backfill sonarr --check-audio [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-audio ...` | `$EXEC --backfill sonarr --check-audio ...` | API, Media, Discord |
| `arr-media-guard --backfill sonarr --check-video [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-video ...` | `$EXEC --backfill sonarr --check-video ...` | API, Media, Discord |
| `arr-media-guard --sub-time PATH ... [--apply]` | `$RUN --sub-time /data/PATH ...` | `$EXEC --sub-time /data/PATH ...` | Media, LID. The API names the original language, and Plex gets the analyze after an edit. It exits 0, 1 (an app lookup failed, nothing changed), 3 (a planned change did not happen) or 4 (a path holds no file). |
| `arr-media-guard --audit sonarr (--plan-from FILE \| --since 24h) [--source hook\|backfill] [--post]` | `$RUN --audit sonarr --since 24h ...` | `$EXEC --audit sonarr --since 24h ...` | API, Media, Discord |
| `arr-media-guard-subhunt radarr --ids ID ... [--apply] [--force]` | `$RUN arr-media-guard-subhunt radarr --ids ID ...` | `docker exec -it arr-media-guard arr-media-guard-subhunt radarr --ids ID ...` | API, Media, LID, `SABNZBD_API_KEY` and `NEWZNAB_API_KEY`, and SABnzbd and NZBHydra2 at the addresses Radarr has saved, or at `SABNZBD_URL` and `NEWZNAB_URL`. The download folder at SABnzbd's path, or in a `RADARR_PATH_MAP` pair. |

- Every `sonarr` above takes `radarr` or the name of another instance too. The subtitle hunter takes a Radarr
  instance only.
- A file a command names, such as `--plan-out` or `--plan-from`, is a path in the container. Put it under `/config` to
  keep it.
- The audit needs Media and the API to remove kept originals, and the state store for the rest.

### Stop a command

`tini` is PID 1 in the image. `docker stop` sends SIGTERM, and Ctrl+C sends SIGINT, to the command's process group, as
in a terminal.

- A scan or a backfill ends the files in flight and stops. The next run resumes at its saved place.
- A command with no stop of its own, such as `--plex-flush`, ends at once. Its exit code is 143 after `docker stop`
  and 130 after Ctrl+C.
- A running flag edit, re-grab or swap ends first.
