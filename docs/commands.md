# Commands

arr-media-guard (AMG) checks each import by itself. These commands run its checks over the files you already have, or on
single files. A backfill and `--sub-time` are dry unless you add `--apply`. A few commands act without it. The audit
cleans up and turns on On Grab, `--plex-flush` asks Plex to scan, and `--test-discord` posts a message. `arr-media-guard
--help` lists the commands, and `--help` after a command lists its options.

In Docker, run each command in the image, see [Commands in Docker](#commands-in-docker).

## Connect to the apps

A command that names an app talks to it through its API, at `RADARR_URL` or `SONARR_URL`. A command also takes the name
of an instance of `APP_INSTANCES`, as `--backfill sonarr-4k`, and uses that instance's keys.

- AMG reads each app's API key from `config.xml` in `RADARR_DIR` or `SONARR_DIR`. For an app whose folder AMG cannot
  see, as in Docker, set `RADARR_API_KEY` or `SONARR_API_KEY`.
- No command reads an app's database. The API key alone serves every command.
- An app counts as set up when its API key reads, or when its URL differs from the URLs the env files ship. An app at a
  shipped URL with no API key counts as not set up, also when you keep that URL on purpose.
- A backfill or a scan of an app that is not set up stops with one line. The audit warns and goes on. `--sub-time` says
  one line when no app at all is set up, and then works on the file alone.

## Dry runs and the backfill

A *backfill* runs the checks of an import over the files already in your library, and fixes their default tracks and
language tags. It runs at the lowest CPU and disk priority, and never posts to Discord. It shares a lock with the
imports, so the two never change one file at the same time. It takes the Matroska files the app lists. It skips a file
with one audio track, no subtitle and a right language tag, which plays that track anyway.

Run a large backfill in steps, and note the time before each apply. A *plan* is what a dry run would change in one file.
A *plan group* is the [item class](policy.md#keys) and the rules of a plan, as `--audit --plan-from` groups them. A
*canary* is a small sample, spread across the plan groups, that you apply first.

```
arr-media-guard --backfill radarr --plan-out /root/radarr-plans.jsonl                        # dry run, one plan per line
arr-media-guard --audit radarr --plan-from /root/radarr-plans.jsonl                          # the plans, by plan group
arr-media-guard --backfill radarr --apply --plan-from /root/radarr-plans.jsonl --canary 20   # 20 files across plan groups
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
arr-media-guard --backfill sonarr --ids 101 --sub-check --recheck   # the same, also the files checked before
arr-media-guard --sub-time "/data/movies/Film A (2000)/Film A (2000).mkv"   # time every subtitle of one file, dry
arr-media-guard --burn-in "/data/movies/Film A (2000)/Film A (2000).mkv"   # look for burned-in subtitles, report only
arr-media-guard-subhunt radarr --ids 123                      # find a release with English subtitles, dry. Needs SABnzbd and NZBHydra2.
arr-media-guard --audit radarr --since 24h --post             # AMG's edits of the last day, to Discord
```

- `--ids` takes the app's own ids: Radarr's movie id or Sonarr's series id, the `id` field of `/api/v3/movie` or
  `/api/v3/series`. A TMDB, TVDB or IMDb id does not work.
- `--paths` limits the backfill, `--convert` and `--sub-check` to the listed files. A scan refuses it.
- `--limit N` stops after N files that need a change. A scan checks N files.
- `--only-undecided` with `--plan-from` takes only the files the plan left undecided, for language detection.
- `--sub-check` and `--sub-time` are the subtitle check, see [subtitles.md](subtitles.md). `--sub-check` runs only with
  the backfill, never with `--convert` or a scan.
- `--sub-check` skips a file whose saved result still holds. `--recheck` checks it again anyway, and needs
  `--sub-check`. With `--ids` or `--paths` it checks only those files. After an update, AMG rechecks some files by
  itself, see [Recheck after an update](features.md#recheck-after-an-update).
- `SCAN_WORKERS` sets the files a dry-run backfill or a scan reads at a time, and `--workers N` wins. A backfill with
  `--apply` changes one file at a time, and `--convert --apply` converts `CONVERT_WORKERS` at a time.
- `--canary` needs `--apply` and `--plan-from`.
- The subtitle hunter's `--force` tries again a film that an earlier run gave up on, or whose import failed.

## Check for burned-in subtitles

`--burn-in` runs the burned-in subtitle check on the files you name, as the check after an import runs it, see
[features.md](features.md#burned-in-subtitles). It runs the quick check and the full check, and the second check when
the full one finds subtitles it can read. It prints one line per file with what it found, and what the check after an
import would do with the current settings, from that check's own decision. It changes no file, posts nothing, and
queues nothing. Each file gets a decision line. It has no `--apply`.

```
arr-media-guard --burn-in "/data/movies/Film A (2000)/Film A (2000).mkv"
arr-media-guard --burn-in --app sonarr --ids 101 102          # every file of these series
```

```
quick check flagged, Japanese audio, full burned-in subtitles in English, the second pass agrees, the job would turn off the English subtitles and alert | Film A (2000) | /data/movies/Film A (2000)/Film A (2000).mkv
```

It needs the language detection install and the text models, and stops with the reason when they are missing. It runs
at the lowest CPU and disk priority. A full check reads the whole audio of a file once, which takes minutes for a film.

## Plex after an apply

After an apply changes a file, AMG asks Plex to read the item again, so Plex shows the new tracks. It waits until the
item's library is idle on two checks 15 seconds apart, because a read during a Plex scan can crash Plex. When "Scan my
library automatically" is off in Plex, the requests that follow soon after need one idle check each. See "The burst" in
[design.md](design.md#plex).

A file that a conversion renamed gets a scan of its folder instead, once per folder at the end of the run.
`--plex-later` on a `--convert --apply` run lists those folders and scans none of them. `--plex-flush` then sends one
Plex scan per Plex library folder that holds them, instead of one scan per changed folder. It needs `PLEX_URL`.

## Scans

`--check-audio` scans the library for broken audio, and `--check-video` for corrupt video. A scan only reads. It never
deletes or re-grabs. It lists what it finds in `STATE_DIR`, and posts one summary to Discord when it finds a problem. It
keeps its place, so a later run goes on where it stopped. `--restart` starts a new pass, and a scan with `--ids` always
starts one.

## Convert files into Matroska

`--convert` takes the files whose name does not end in `.mkv`, and converts them into Matroska with the same streams.
With `CONVERT=true`, AMG also converts each such import. A file named `.mkv` that holds another container converts in
every run, with no setting. `CONVERT_MAX_FILES`, `CONVERT_WORKERS` and `REPACK_MAX_GB` set the limits, see
[the settings](../README.md#repairs-and-conversion).

### Force a conversion

Before AMG removes the source file, it checks the new file against it, stream by stream. It refuses a file it cannot
prove has lost nothing, and the decision log holds the reason. When you checked the reason and accept it, name the file.

```
arr-media-guard --backfill radarr --convert --apply --force-convert "/data/movies/Film A (2000)/Film A (2000).mp4"
```

- The run then takes only the listed files.
- A file converts when the check refuses it for the same reason as the last refusal in the log. AMG keeps the decision
  lines of 14 days for this. Another reason, or no logged refusal, is not forced, and the run says why.
- Every other step runs as normal.
- The original stays in `.<NAME>-originals` for `KEEP_ORIGINALS_DAYS`, so one move puts it back.
- The decision line names the refusal in `repack.forced`. On a host, the audit's printed list marks the file as forced.
- The option needs `--convert --apply` and `KEEP_ORIGINALS_DAYS` above 0.
- A path that is not in the run's list of files is reported and skipped.

A listed Sonarr file also skips the check that Sonarr reads the new file name as the same episodes. Scene numbering or
an alias can make that check wrong while the names are right. The conversion names the episodes by their ids, so the
link stays right. This needs no logged refusal.

- The extras, such as subtitles and `.nfo` files, keep that check. AMG takes them from the whole series folder. An extra
  whose name reads as other episodes still refuses the file and is named, so you can fix or remove it.
- The stream check still runs, and its refusal is forced only as above.
- The decision line names what Sonarr read in `repack.forced_name`, and the original stays in `.<NAME>-originals`. On a
  host, the audit's printed list says "name forced".

## The audit

`--audit` reviews changes. With `--plan-from` it groups the plans of a dry run by the rule that made them. With
`--since` it reviews the changes that imports and backfills with `--apply` made in that time. AMG keeps the decision
lines for 14 days. `--source hook` takes only the imports, and `--source backfill` only the backfills. `--post` sends
the summary to Discord when the audit found a problem.

Schedule the nightly audit of each app on a host install, with a systemd timer or cron:

```
arr-media-guard --audit radarr --since 24h --post
arr-media-guard --audit sonarr --since 24h --post
```

Add one line for each instance of `APP_INSTANCES`.

Every audit with `--since` also does this, with no other option.

- It removes the kept originals and the `KEEP_REPLACED` copies older than `KEEP_ORIGINALS_DAYS`. At 0 it removes every
  `KEEP_REPLACED` copy, and leaves the originals already kept.
- It turns on On Grab in the app's connection to AMG, where `KEEP_REPLACED` needs it.
- On a host, the first audit of a new version queues the rechecks of that version, see
  [Recheck after an update](features.md#recheck-after-an-update).

In Docker, the listener runs the nightly audit at `AUDIT_TIME`, see [docker.md](docker.md#the-listener).

## The selftest

`--selftest` checks the settings and the policy file, then each app whose API key reads, then each other service the
setup uses. A bad setting, an env file that does not read, or a policy file that does not load fails it. An app or a
service that fails its check only warns, because a deploy may run the selftest while one is down. The last line is
`selftest ok`.

```
arr-media-guard --selftest
```

### Service checks

The selftest and the listener's start check ask each service the setup uses once, and change nothing. Each request waits
at most 10 seconds.

| Service | Checked when | Request | A warning says |
| --- | --- | --- | --- |
| Plex | `PLEX_URL` is set | the list of libraries, with `PLEX_TOKEN` | Plex did not answer, or it refused `PLEX_TOKEN` |
| Discord | `DISCORD_WEBHOOK` is set | the webhook's details, which Discord gives without a post | Discord did not answer, it does not know the webhook, or it refused the webhook's token |
| TMDB | always | TMDB's key check, with `TMDB_TOKEN`, else Radarr's key | TMDB did not answer, or it refused the key |
| SABnzbd | `SABNZBD_API_KEY` is set | one slot of the queue, at `SABNZBD_URL` or the address Radarr saved | SABnzbd did not answer, it refused `SABNZBD_API_KEY`, or it refused the request, as from an address it does not let in |
| Newznab indexer | `NEWZNAB_API_KEY` is set | `t=caps`, the indexer's list of features, at `NEWZNAB_URL` or the address Radarr saved | the indexer did not answer, or it refused `NEWZNAB_API_KEY` |
| Burned-in subtitle check | `BURNED_IN` is not `off` | none. It looks for the language detection install, and checks the sha256 of each text model | the check cannot run, and why |

```
arr-media-guard: plex start check: ok
arr-media-guard: discord warning: Discord refused the token in DISCORD_WEBHOOK
arr-media-guard: tmdb start check: ok
```

The selftest prints only the warnings, as `warning: Discord refused the token in DISCORD_WEBHOOK`.

- No line shows a token, a key or the webhook URL.
- No check posts to Discord, so the channel gets no message at a start. `--test-discord` posts one.
- No check sends a search to the indexer. A search costs each indexer behind NZBHydra2 a hit of its daily limit.
- NZBHydra2 checks the key before it answers `t=caps`. Another Newznab indexer may answer without a key, so there the
  check proves the address only.
- Plex takes any token from a network in its list of networks allowed without auth, in Settings, Network. From there the
  check proves the address only.
- SABnzbd and the indexer count only for a Radarr instance whose API key reads, because the subtitle hunter reads their
  addresses from Radarr. Each such Radarr gets its own check. A Radarr with no SABnzbd client or indexer, or one saved
  with no address, warns of that.
- The start check asks an app or a service that does not answer again, for 2 minutes in all. The selftest asks once.
- A token or an address that AMG cannot send, such as a token with a curly apostrophe, warns at once. The Plex, Discord
  and TMDB lines name the setting, and the SABnzbd and indexer lines name the address.
- The start check reads the Plex library folders after the Plex check, so a Plex that starts beside the listener has its
  folders checked.

## Test the Discord webhook

`--test-discord` posts one short test message to `DISCORD_WEBHOOK`. When Discord takes it, AMG prints the HTTP status.
Otherwise it prints the error and exits 1. A refusal shows Discord's own message and code, and leaves out the answer of
any other server. With no `DISCORD_WEBHOOK` it posts nothing and exits 1. The output never shows the webhook.

```
arr-media-guard --test-discord
```

## Commands in Docker

Every command of a host install runs in the image, as a one-off container or with `docker exec`.

- Both run as `PUID:PGID`. The exit code of the command is the exit code of `docker run` and `docker exec`.
- A one-off container mounts the service's `/config` and its volume `amg-state`. So it shares the same lock and the same
  records as the service.
- Run a long command, such as a backfill, a conversion or a scan, as a one-off container. A restart of the service, as
  an image update does, ends every `docker exec` process in it.
- With the compose install, `$RUN` is `docker compose run`. Run it in the folder of the compose file. It takes the
  environment and the mounts of the compose file.
- With a `docker run` install, `$RUN` needs every `-e` key and mount of your `docker run` command. Without the API keys,
  a one-off command reads an empty key.

```
RUN="docker compose run --rm arr-media-guard"   # the compose install
RUN="docker run --rm -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -e RADARR_URL=http://CHANGE_ME:7878 -e RADARR_API_KEY=CHANGE_ME \
  -e SONARR_URL=http://CHANGE_ME:8989 -e SONARR_API_KEY=CHANGE_ME \
  -v /srv/appdata/arr-media-guard:/config -v amg-state:/config/state -v /srv/media:/data \
  ghcr.io/samwiseg0/arr-media-guard:latest"   # a docker run install, with the values of your command
EXEC="docker exec -it arr-media-guard arr-media-guard"
```

Keep `-it` on `docker exec`, so Ctrl+C reaches the command, and leave it out in a script.

A one-off container writes its summary lines to syslog only, and the image runs no syslog. To keep them, as for Loki,
mount `/dev/log`. The `/dev/log` line of the compose file does it for `docker compose run` and `docker exec`. With
`docker run`, add `-v /dev/log:/dev/log` to `$RUN` and to the service.

Each command needs `/config`. The last column of the table names what else it needs:

- **API**: the app's URL and `<APP>_API_KEY`.
- **Media**: the media at the apps' paths, or a path map. An apply writes there.
- **LID**: language detection, which the image holds.
- **Plex** and **Discord**: `PLEX_URL` and `PLEX_TOKEN`, and `DISCORD_WEBHOOK`. Without them most commands skip the Plex
  request or the post. `--plex-flush` with no `PLEX_URL` stops with an error.

| Host command | One-off container | `docker exec` | Needs |
| --- | --- | --- | --- |
| `arr-media-guard` | none, the Webhook listener takes its place | none | |
| `arr-media-guard --serve` | the service itself | none | API, Media, LID, Plex, Discord |
| `arr-media-guard --selftest` | `$RUN --selftest` | `$EXEC --selftest` | |
| `arr-media-guard --test-discord` | `$RUN --test-discord` | `$EXEC --test-discord` | Discord |
| `arr-media-guard --backfill sonarr [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--plan-out FILE] [--workers N]` | `$RUN --backfill sonarr ...` | `$EXEC --backfill sonarr ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --sub-check [--apply] [--recheck] [--ids ID ...] [--paths PATH ...]` | `$RUN --backfill sonarr --sub-check ...` | `$EXEC --backfill sonarr --sub-check ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --apply --plan-from FILE [--canary N]` | `$RUN --backfill sonarr --apply --plan-from /config/FILE ...` | `$EXEC --backfill sonarr --apply --plan-from /config/FILE ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --plan-from FILE --only-undecided [--apply] [--plan-out FILE]` | `$RUN --backfill sonarr --plan-from /config/FILE --only-undecided ...` | `$EXEC --backfill sonarr --plan-from /config/FILE --only-undecided ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --convert [--apply] [--plan-from FILE] [--canary N] [--workers N] [--plex-later]` | `$RUN --backfill sonarr --convert ...` | `$EXEC --backfill sonarr --convert ...` | API, Media, Plex |
| `arr-media-guard --backfill sonarr --convert --apply --force-convert PATH ...` | `$RUN --backfill sonarr --convert --apply --force-convert PATH ...` | `$EXEC --backfill sonarr --convert --apply --force-convert PATH ...` | API, Media, Plex |
| `arr-media-guard --plex-flush sonarr` | `$RUN --plex-flush sonarr` | `$EXEC --plex-flush sonarr` | Plex |
| `arr-media-guard --backfill sonarr --check-audio [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-audio ...` | `$EXEC --backfill sonarr --check-audio ...` | API, Media, Discord |
| `arr-media-guard --backfill sonarr --check-video [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-video ...` | `$EXEC --backfill sonarr --check-video ...` | API, Media, Discord |
| `arr-media-guard --sub-time PATH ... [--apply]` | `$RUN --sub-time /data/PATH ...` | `$EXEC --sub-time /data/PATH ...` | Media, LID. The API names the original language, and Plex gets the request after a change. It exits 0, 1 (with `--apply` an app lookup failed and nothing changed, or the policy file did not load), 2 (a wrong option), 3 (with `--apply` a planned change did not happen) or 4 (a path holds no file). |
| `arr-media-guard --burn-in (PATH ... \| --app sonarr --ids ID ...)` | `$RUN --burn-in /data/PATH ...` | `$EXEC --burn-in /data/PATH ...` | Media, LID. With `--app`: API. |
| `arr-media-guard --audit sonarr (--plan-from FILE \| --since 24h) [--source hook\|backfill] [--post]` | `$RUN --audit sonarr --since 24h ...` | `$EXEC --audit sonarr --since 24h ...` | With `--since`: API, Media, Discord. With `--plan-from`: none. |
| `arr-media-guard-subhunt radarr --ids ID ... [--apply] [--force]` | `$RUN arr-media-guard-subhunt radarr --ids ID ...` | `docker exec -it arr-media-guard arr-media-guard-subhunt radarr --ids ID ...` | API, Media, LID, Discord, `SABNZBD_API_KEY` and `NEWZNAB_API_KEY`, and SABnzbd and NZBHydra2 at the addresses Radarr has saved, or at `SABNZBD_URL` and `NEWZNAB_URL`. The download folder at SABnzbd's path, or in a pair of Radarr's path map. |

- Every `sonarr` above takes `radarr` or the name of another instance too. The subtitle hunter takes a Radarr instance
  only.
- A file a command names, such as `--plan-out` or `--plan-from`, is a path in the container. Put it under `/config` to
  keep it.
- The audit with `--since` needs the API and the media. It uses them to remove kept files, to check a changed file
  again, and to turn on On Grab. Its review reads AMG's records.

### Stop a command

`docker stop` and Ctrl+C stop the command as they would in a terminal.

- A scan stops the files it was reading, and the next run reads them again. It keeps its place, so the next run goes on
  from there.
- A backfill with `--apply` lets the files in flight finish, then stops. A dry run stops at once. A backfill keeps no
  place, so the next run goes through the whole list again. It finds nothing to change in the files already done.
- A conversion in progress with one worker stops, and the original stays as it was.
- A command with no stop of its own, such as `--plex-flush`, ends at once. Its exit code is 143 after `docker stop` and
  130 after Ctrl+C.
- A running flag edit, re-grab, or the last step of a conversion always finishes first.
