# Commands

arr-media-guard (AMG) checks each import by itself. These commands run its checks over the files you already have, or on
single files. A backfill, `--sub-time` and `--check` are dry unless you add `--apply`. A few commands act without it. The audit
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
- A backfill or a scan of an app that is not set up stops with one line. A backfill with no instance skips it. The audit
  warns and goes on. `--sub-time` says one line when no app at all is set up, and then works on the file alone.

## Dry runs and the backfill

A *backfill* runs the checks of an import over the files already in your library. Run it first with no option. It
names each file the apps list and the hours of video they hold, and changes nothing. Then add `--apply`.

```
docker exec -it arr-media-guard arr-media-guard --backfill           # dry run of every instance
docker exec -it arr-media-guard arr-media-guard --backfill --apply   # queue every file
```

On a host, leave out `docker exec -it arr-media-guard`.

- `--apply` puts one job per file in the background queue, where the deep analyses and the rechecks wait. The worker
  runs each job as `--check PATH --apply` runs the file, see [Check one file](#check-one-file). So the job does what an
  import does under your settings, and posts its alerts. It never re-grabs. Its conversion keeps no original, as an
  import's does.
- The jobs wait behind the imports and run one at a time, after the deep analyses of imports and the rechecks. A job
  gives way to an import that waits before a conversion and before a remux, until it changed the file. The jobs stay
  in the queue across a restart. `--backfill --drop-queued` drops them, and `--backfill <instance> --drop-queued`
  drops those of one instance. A job that runs then ends first.
- A job queues the burned-in subtitle check and the deep analysis as an import does. The deep analysis waits behind
  every job a backfill queued. At `SUBTITLES=deep` it takes a median of about 5 minutes per hour of video, and one file
  in four takes 15 minutes or more per hour. A file whose saved deep analysis still holds gets no new one.
- A backfill keeps its place in each instance, as a scan does. `--limit N --apply` queues the next N files, and the next
  run goes on from there. A run after the last file queues nothing and says so. `--restart` starts a new pass, as after
  a change of your settings. `--ids` and `--paths` keep a place of their own. A dry run keeps no place and names the
  next files it would queue.
- A file that has a job waiting or running keeps that job, so a second `--apply` queues it once. A file with a waiting
  recheck, or one missing on disk, stays for the next run.
- A first backfill posts each problem that was not posted yet, once per file and kind. One file can post up to three
  times: from its job, its burned-in subtitle check and its deep analysis. With `DISCORD_POSTS=all`, each file whose
  default tracks change also posts "Default tracks changed".
- With no instance it takes every instance in turn, `radarr`, `sonarr`, then those of `APP_INSTANCES`. A line
  `== <instance>` starts each one. It skips an instance you did not set up. An instance that fails, as one that does
  not answer, ends with one line, and the run goes on to the next one. The exit code is then 1.
- On a host the worker starts with the next event of an app, as an import or a Test.

A plan option, `--plan-out` or `--plan-from`, runs the *flag backfill* instead. It fixes the default tracks and
language tags of the Matroska files the app lists, at the lowest CPU and disk priority, and never posts to Discord. It
shares a lock with the imports, so the two never change one file at the same time. It skips a file with one audio
track, no subtitle and a right language tag, which plays that track anyway.

Run a large flag backfill in steps, and note the time before each apply. A *plan* is what a dry run would change in one
file. A *plan group* is the [item class](policy.md#keys) and the rules of a plan, as `--audit --plan-from` groups them.
A *canary* is a small sample, spread across the plan groups, that you apply first.

```
arr-media-guard --backfill radarr --plan-out /config/plans/radarr.jsonl                        # dry run, one plan per line
arr-media-guard --audit radarr --plan-from /config/plans/radarr.jsonl                          # the plans, by plan group
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl --canary 20   # 20 files across plan groups
arr-media-guard --audit radarr --since 2026-01-01T10:00                                        # check the canary's edits
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl               # the rest
```

With no instance, one plan file holds the plans of every instance, and `--canary N` takes N files of each.

Other useful commands:

```
arr-media-guard --backfill sonarr --ids 101 102               # only these series (movie ids for radarr), dry
arr-media-guard --backfill radarr --apply --limit 5           # queue the next five files
arr-media-guard --backfill radarr --paths "/data/movies/Film A (2000)/Film A (2000).mkv"   # one file, dry
arr-media-guard --backfill sonarr --check-audio --limit 2000  # scan for broken audio, resumes where it stopped
arr-media-guard --backfill sonarr --check-video --restart     # scan for corrupt video, a new pass
arr-media-guard --backfill sonarr --convert                   # the files that are not .mkv, dry
arr-media-guard --backfill sonarr --convert --apply           # convert them, CONVERT_MAX_FILES a run
arr-media-guard --backfill sonarr --ids 101 --sub-check       # check the subtitles of one series against the audio, dry
arr-media-guard --backfill sonarr --ids 101 --sub-check --recheck   # the same, also the files checked before
arr-media-guard --sub-time "/data/movies/Film A (2000)/Film A (2000).mkv"   # time every subtitle of one file, dry
arr-media-guard --check "/data/movies/Film A (2000)/Film A (2000).mkv"      # every check of an import on one file, dry
arr-media-guard --burn-in "/data/movies/Film A (2000)/Film A (2000).mkv"   # look for burned-in subtitles, report only
arr-media-guard-subhunt radarr --ids 123                      # find a release with English subtitles, dry. Needs SABnzbd and NZBHydra2.
arr-media-guard --audit radarr --since 24h --post             # AMG's edits of the last day, to Discord
```

- `--ids` takes the app's own ids: Radarr's movie id or Sonarr's series id, the `id` field of `/api/v3/movie` or
  `/api/v3/series`. A TMDB, TVDB or IMDb id does not work. It needs an instance.
- `--paths` limits the backfill, `--convert` and `--sub-check` to the listed files. It needs an instance. A scan
  refuses it.
- `--limit N` stops after N files that need a change. A scan checks N files, and a queue takes the next N files of
  each instance.
- `--only-undecided` with `--plan-from` takes only the files the plan left undecided, for language detection.
- `--sub-check` and `--sub-time` are the subtitle check, see [subtitles.md](subtitles.md). `--sub-check` runs only with
  the backfill, never with `--convert` or a scan.
- `--sub-check` skips a file whose saved result still holds. `--recheck` checks it again anyway, and needs
  `--sub-check`. With `--ids` or `--paths` it checks only those files. After an update, AMG rechecks some files by
  itself, see [Recheck after an update](features.md#recheck-after-an-update).
- `SCAN_WORKERS` sets the files a dry-run backfill or a scan reads at a time, and `--workers N` wins. A backfill with
  `--apply` changes one file at a time, and `--convert --apply` converts `CONVERT_WORKERS` at a time.
- `--canary` needs `--apply` and `--plan-from`. It takes N files of each instance.
- The subtitle hunter's `--force` tries again a film that an earlier run gave up on, or whose import failed.

## Check one file

`--check` runs the checks of an import on each file you name, one file at a time. Use it to see what an import would do
with a file, or to fix a file in place. Run it dry first, and read what it says. Then add `--apply`.

```
arr-media-guard --check "/data/movies/Film A (2000)/Film A (2000).mkv"           # dry run: changes nothing
arr-media-guard --check "/data/movies/Film A (2000)/Film A (2000).mkv" --apply   # changes the file as an import does
```

- It runs each check that your settings turn on for an import. These are the audio and video checks, wrong content and
  the runtime checks, and the burned-in subtitle check. It also runs the subtitle checks at `SUBTITLES` with the deep
  analysis, and it sets the language tags and flags. It repairs a broken header, and converts at `CONVERT` with the
  damage check.
- An import queues the full burned-in subtitle check and the deep analysis to run later. `--check` runs them right after
  the other checks, in the same way, and prints a line for each. They follow the same rules for a dry run and for
  `--apply`. They never wait for an import. The deep analysis has no time limit, so a film can take many minutes.
- Each file runs as an import of its own, with an import's 300-second time limit and the file lock. It takes every file
  you name, also one that a backfill leaves out. It runs at the lowest CPU and disk priority, as a backfill does.
- A dry run changes no file, posts nothing, asks Plex nothing and writes nothing to the app or to `status.json`.
- For each file it prints the label and the whole result, then the plan and the alerts. The result of a converted file
  starts with `repacked`. A `checked:` line gives the result of each check that ran. A `not checked:` line names each
  check that an import runs and this run left out. A burned-in subtitle check with no models is one example. A full
  decode of the audio that the time limit left out is another. The decision line keeps them in `not_checked`.
- It never re-grabs. A file that an import would re-grab gets its second check, and the alert title ends "would
  re-grab". For a kind that `REGRAB` leaves out, the title ends "re-grab is off", as on an import.
- A dry run does not convert. It finds a damaged source only when ffprobe cannot read the file. An `.mkv` file that
  holds another container then gets no other check, as on an import. A `not checked:` line says so.
- The apps name the item of each file, as for `--sub-time`. `--app <instance>` takes the item from that instance only.
  AMG still asks the other instances about a file it does not list. When another instance lists the file, AMG names
  that instance and leaves the file out. With the wrong `--app`, the file would run as a file outside the library.
- AMG names each path that holds no file, and leaves it out. It also names a lookup that fails. The other files still
  run, and the exit code is then 1. When a lookup fails and a path is left that no app lists, `--apply` stops before
  any change. The file may belong to that app.
- A stop with Ctrl+C or SIGTERM during the deep analysis drops the alerts that the check held for it. The next run
  posts them.
- Each run gets a decision line with source `check`.

### What `--apply` changes

- It changes the file as an import does. It posts the alerts to Discord and asks Plex to analyze the file. After a remux
  the app rescans the item.
- A remux and a header repair keep the original in the kept originals, as on an import. So does a conversion, and the
  app then takes the new name. The sidecar subtitles that the new file holds go to the kept originals too, beside the
  original. An import deletes the original and those sidecars.
- A conversion that cannot keep its original is skipped before the remux. The dry run says so too.
- A sidecar subtitle that does not match the audio moves to the kept originals, as on an import.
- The nightly audit removes the kept originals after `KEEP_ORIGINALS_DAYS`. At `0` AMG keeps nothing. A remux of the
  subtitles then keeps every track. A conversion of a file that an app lists deletes the original and those sidecars,
  as an import does.
- Nothing else moves a file out of its folder. Only a conversion at `KEEP_ORIGINALS_DAYS=0` deletes a file.
- The deep analysis holds the file lock exclusive for its remux, at the lowest priority, and never yields. Every import
  waits until the remux ends.

### A file outside the library

A file that no app lists runs too, such as a copy in a scratch folder. `--apply` makes no app write. There is no
rescan, no monitor and no import of a new name. Plex gets an analyze only for a file it lists.

- A conversion writes the new `.mkv` beside the original, and the original stays where it is. A sidecar subtitle that
  does not match the audio moves to the kept originals. The other sidecars stay beside the original, and the new file
  holds them too. So a player that finds them beside the `.mkv` may list each one twice.
- An `.mkv` file that holds another container converts under its own name. Its original goes to the kept originals.
  At `KEEP_ORIGINALS_DAYS=0` AMG keeps nothing, so the original is gone.
- A remux keeps the original in `.<NAME>-originals` at the top of the file's mount. When AMG cannot write there, it
  takes the highest folder on the path that it can write.

Such a file has no item, unless you name one. With no item, AMG knows no original language. So the flags and the
language tags stay as they are, and only a subtitle verdict turns a flag off. Some checks need the item, so they do not
run. These are wrong content, the runtime checks and the episode title check. The audio language alert and the
cut-file test of the audio check need it too. With no item, no app names an alert. So AMG prints the alerts and posts
nothing. Every other check runs.

Name the item to run the other checks too. The `not checked:` lines name the checks that still need an item.

```
arr-media-guard --check /data/scratch/Film.mkv --app radarr --id 123   # Radarr's movie 123
arr-media-guard --check /data/scratch/Show.S01E03.mkv --app sonarr --id 4567   # Sonarr's episode 4567
arr-media-guard --check /data/scratch/Film.mkv --tmdb movie/603   # TMDB's film 603, with no app
arr-media-guard --check /data/scratch/Show.S01E03.mkv --tmdb tv/1396/1/3   # TMDB's series 1396, season 1, episode 3
```

- Run it dry first. The line "no app lists it, so it runs as ..." names the item. Check that it is the right one, then
  add `--apply`.
- `--id` takes Radarr's movie id or Sonarr's episode id, and needs `--app`. The app gives the item as for a file it
  lists.
- `--tmdb` takes the end of TMDB's own URL. A film is `movie/603`. An episode is `tv/1396/1/3`, with the series, the
  season and the episode number. It needs no app.
- TMDB gives the title, the original title, the year, the original language, the runtime, the genres and the studio.
  For an episode it also gives the title and the runtime. AMG asks TMDB with the key of the content checks,
  `TMDB_TOKEN` or Radarr's.
- With `--tmdb`, the run takes the instance of `--app` when it fits the kind. That is a Radarr for a film or a Sonarr
  for an episode. Else it takes `radarr` for a film and `sonarr` for an episode. The alerts name that instance.
- With `--tmdb`, the kids rule reads the genres and the studio. The kids quality profile does not count. TMDB's series
  genre Kids counts as Sonarr's Children. The episode title check compares the titles of the episode's own season only.
  The year check knows none of the other release years that Radarr lists. An anime has no absolute episode numbers,
  and an alert has no link to the item in the app.
- `--id` and `--tmdb` exclude each other. Each one names the item of one file. So only one of the paths may be a file
  that no app lists. A file that an app lists keeps the app's own item, and the run prints one line that says so.
- A file that no app lists counts in no app's nightly audit.

## Check for burned-in subtitles

`--burn-in` runs the burned-in subtitle check on the files you name, the same way it runs after an import, see
[features.md](features.md#burned-in-subtitles). It runs the quick check, the full check, and the second check when the
full one finds subtitles it can read. For each file it prints one line with what it found, and what AMG would do after
an import with your current settings. It changes no file, posts nothing and queues nothing. Each file gets a line in
the decision log. There is no `--apply`.

```
arr-media-guard --burn-in "/data/movies/Film A (2000)/Film A (2000).mkv"
arr-media-guard --burn-in --app sonarr --ids 101 102          # every file of these series
```

```
quick check flagged, Japanese audio, full burned-in subtitles in English, the second pass agrees, the job would turn off the English subtitles and alert | Film A (2000) | /data/movies/Film A (2000)/Film A (2000).mkv
```

It needs the language detection install and the text models, and tells you what is missing when it cannot run. It
runs at the lowest CPU and disk priority. The full check reads the whole audio of each file, so a film takes a few
minutes.

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
`--since` it reviews the changes that imports, backfills and `--check` with `--apply` made in that time. A file that no
app lists counts in no audit. AMG keeps the decision lines for 14 days. `--source hook` takes only the imports,
`--source backfill` only the backfills, and `--source check` only `--check`. `--post` sends the summary to Discord when
the audit found a problem.

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
| Burned-in subtitle check | `BURNED_IN` is not `off` | none. It checks that language detection is installed, and that each text model matches its sha256 | the check cannot run, and why |

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
| `arr-media-guard --backfill [sonarr] [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--restart]` | `$RUN --backfill ...` | `$EXEC --backfill ...` | API. The jobs need what the service needs. |
| `arr-media-guard --backfill [sonarr] --drop-queued` | `$RUN --backfill --drop-queued` | `$EXEC --backfill --drop-queued` | |
| `arr-media-guard --backfill sonarr --plan-out FILE [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--workers N]` | `$RUN --backfill sonarr --plan-out /config/plans/FILE ...` | `$EXEC --backfill sonarr --plan-out /config/plans/FILE ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --sub-check [--apply] [--recheck] [--ids ID ...] [--paths PATH ...]` | `$RUN --backfill sonarr --sub-check ...` | `$EXEC --backfill sonarr --sub-check ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --apply --plan-from FILE [--canary N]` | `$RUN --backfill sonarr --apply --plan-from /config/plans/FILE ...` | `$EXEC --backfill sonarr --apply --plan-from /config/plans/FILE ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --plan-from FILE --only-undecided [--apply] [--plan-out FILE]` | `$RUN --backfill sonarr --plan-from /config/plans/FILE --only-undecided ...` | `$EXEC --backfill sonarr --plan-from /config/plans/FILE --only-undecided ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --convert [--apply] [--plan-from FILE] [--canary N] [--workers N] [--plex-later]` | `$RUN --backfill sonarr --convert ...` | `$EXEC --backfill sonarr --convert ...` | API, Media, Plex |
| `arr-media-guard --backfill sonarr --convert --apply --force-convert PATH ...` | `$RUN --backfill sonarr --convert --apply --force-convert PATH ...` | `$EXEC --backfill sonarr --convert --apply --force-convert PATH ...` | API, Media, Plex |
| `arr-media-guard --plex-flush sonarr` | `$RUN --plex-flush sonarr` | `$EXEC --plex-flush sonarr` | Plex |
| `arr-media-guard --backfill sonarr --check-audio [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-audio ...` | `$EXEC --backfill sonarr --check-audio ...` | API, Media, Discord |
| `arr-media-guard --backfill sonarr --check-video [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-video ...` | `$EXEC --backfill sonarr --check-video ...` | API, Media, Discord |
| `arr-media-guard --sub-time PATH ... [--apply]` | `$RUN --sub-time /data/PATH ...` | `$EXEC --sub-time /data/PATH ...` | Media, LID. The API names the original language, and Plex gets the request after a change. It exits 0, 1 (with `--apply` an app lookup failed and nothing changed, or the policy file did not load), 2 (a wrong option), 3 (with `--apply` a planned change did not happen) or 4 (a path holds no file). |
| `arr-media-guard --check PATH ... [--app sonarr] [--id ID \| --tmdb KIND/ID] [--apply]` | `$RUN --check /data/PATH ...` | `$EXEC --check /data/PATH ...` | Media, LID. The API names the item, and Plex and Discord get the requests of an apply. It exits 0, 1 (a path holds no file, another instance than `--app` lists it, a lookup failed, an item did not read, a check ended in an error, or the policy file did not load) or 2 (a wrong option, or `--id` or `--tmdb` with more than one path that no app lists). |
| `arr-media-guard --burn-in (PATH ... \| --app sonarr --ids ID ...)` | `$RUN --burn-in /data/PATH ...` | `$EXEC --burn-in /data/PATH ...` | Media, LID. With `--app`: API. |
| `arr-media-guard --audit sonarr (--plan-from FILE \| --since 24h) [--source hook\|backfill\|check] [--post]` | `$RUN --audit sonarr --since 24h ...` | `$EXEC --audit sonarr --since 24h ...` | With `--since`: API, Media, Discord. With `--plan-from`: none. |
| `arr-media-guard-subhunt radarr --ids ID ... [--apply] [--force]` | `$RUN arr-media-guard-subhunt radarr --ids ID ...` | `docker exec -it arr-media-guard arr-media-guard-subhunt radarr --ids ID ...` | API, Media, LID, Discord, `SABNZBD_API_KEY` and `NEWZNAB_API_KEY`, and SABnzbd and NZBHydra2 at the addresses Radarr has saved, or at `SABNZBD_URL` and `NEWZNAB_URL`. The download folder at SABnzbd's path, or in a pair of Radarr's path map. |

- Every `sonarr` above takes `radarr` or the name of another instance too. A backfill or a scan with no instance runs
  every instance. The subtitle hunter takes a Radarr instance only.
- A file a command names, such as `--plan-out` or `--plan-from`, is a path in the container. Put it under `/config` to
  keep it. `--plan-out` makes its folder.
- The audit with `--since` needs the API and the media. It uses them to remove kept files, to check a changed file
  again, and to turn on On Grab. Its review reads AMG's records.

### Stop a command

`docker stop` and Ctrl+C stop the command as they would in a terminal.

- A scan stops the files it was reading, and the next run reads them again. It keeps its place, so the next run goes on
  from there.
- A backfill with `--apply` lets the files in flight finish, then stops. A dry run stops at once. A stop ends the whole
  run, so the next instance does not start. A flag backfill keeps no place, so the next run goes through the whole
  list again. It finds nothing to change in the files already done. A backfill that queues keeps its place.
- A conversion in progress with one worker stops, and the original stays as it was.
- A command with no stop of its own, such as `--plex-flush`, ends at once. Its exit code is 143 after `docker stop` and
  130 after Ctrl+C.
- A running flag edit, re-grab, or the last step of a conversion always finishes first.
