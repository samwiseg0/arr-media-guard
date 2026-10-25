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
  import does under your settings, and posts its alerts. The "Variable frame rate" alert is the exception. A job only
  logs it in `video.frame_rate`. It never re-grabs. Its conversion keeps no original, as an import's does.
- The jobs wait behind the imports and run `BACKGROUND_WORKERS` at a time, after the deep analyses of imports and the
  rechecks. A job
  gives way to an import that waits before a conversion and before a remux, until it changed the file. The jobs stay
  in the queue across a restart. `--backfill --drop-queued` drops them, and `--backfill <instance> --drop-queued`
  drops those of one instance. A job that runs then ends first.
- A job queues the burned-in subtitle check, the whole-file check and the deep analysis as an import does. The
  whole-file check reads each file whole, so a full backfill reads the library once more, and it only alerts. The deep
  analysis waits behind every job a backfill queued. At `SUBTITLES=deep` it takes about 16 minutes per hour of video
  on a recent CPU, see [design.md](design.md#subtitle-match). A file whose saved deep analysis still holds gets no
  new one. `--selftest` counts these checks as jobs from a backfill, as in
  `background queue: 12 jobs, 9 of them from a backfill (1 file still to check)`.
- A backfill keeps its place in each instance, as a scan does. `--limit N --apply` queues the next N files, and the next
  run goes on from there. A run after the last file queues nothing and says so. `--restart` starts a new pass, as after
  a change of your settings. `--ids` and `--paths` keep a place of their own. A dry run keeps no place and names the
  next files it would queue.
- A file that has a job waiting or running keeps that job, so a second `--apply` queues it once. A file with a waiting
  recheck, or one missing on disk, stays for the next run.
- Each apply that queues, here or with `--background`, writes one decision log line with result `queued`, its counts
  and its options. An apply that stops part way, as at Ctrl+C, writes the line with the counts so far.
- A first backfill posts each problem that was not posted yet, once per file and kind. One file can post up to four
  times: from its job, its burned-in subtitle check, its whole-file check and its deep analysis. With
  `DISCORD_POSTS=all`, each file whose default tracks change also posts "Default tracks changed".
- With no instance it takes every instance in turn, `radarr`, `sonarr`, then those of `APP_INSTANCES`. A line
  `== <instance>` starts each one. It skips an instance you did not set up. An instance that fails, as one that does
  not answer, ends with one line, and the run goes on to the next one. The exit code is then 1.
- On a host the worker starts with the next import of an app. `--queue` shows the jobs that run, see
  [The queue](#the-queue).

A plan option, `--plan-out` or `--plan-from`, runs the *flag backfill* instead. It fixes the default tracks and
language tags of the Matroska files the app lists, at the lowest CPU and disk priority. Its `--apply` posts as a job
above does. It posts each problem not posted yet, once per file and kind. With `DISCORD_POSTS=all` it posts each
change too. A dry run posts nothing. It shares a lock with the imports, so the two never change one file at the same
time. It skips a file only when no check that can write has work for it, as a file with one audio track, its flags
and language tag right, no subtitle and a header with no issue. Each such check has one trigger in `cli.TRIGGERS`.
A new check that can write adds its trigger there.

Run a large flag backfill in steps, and note the time before each apply. A *plan* is what a dry run would change in one
file. A *plan group* is the [item class](policy.md#keys) and the rules of a plan, as `--audit --plan-from` groups them.
A *canary* is a small sample, spread across the plan groups, that you apply first. At `DISCORD_POSTS=all` an apply
posts once or more for each file it changes.

```
arr-media-guard --backfill radarr --plan-out /config/plans/radarr.jsonl                        # dry run, one plan per line
arr-media-guard --audit radarr --plan-from /config/plans/radarr.jsonl                          # the plans, by plan group
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl --canary 20   # 20 files across plan groups
arr-media-guard --audit radarr --since 2026-01-01T10:00                                        # the open problems since the canary
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl               # the rest
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl --only-header # or only the header repairs, padding left
```

With no instance, one plan file holds the plans of every instance, and `--canary N` takes N files of each.

### Apply a plan in the background queue

`--background` puts each file of an audited plan in the background queue, with its plan. The worker applies the plan
after the imports, in the places of `BACKGROUND_WORKERS`. Without it, `--apply --plan-from` and `--check --apply` edit
the files in their own process, beside the imports.

```
arr-media-guard --backfill radarr --plan-out /config/plans/radarr.jsonl                                     # dry run
arr-media-guard --audit radarr --plan-from /config/plans/radarr.jsonl                                       # the plans, by plan group
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl --background --canary 20   # queue 20 files
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr.jsonl --background               # queue the rest
arr-media-guard --check "/data/movies/Film A (2000)/Film A (2000).mkv" --plan-out /config/plans/film-a.jsonl                 # dry run
arr-media-guard --check "/data/movies/Film A (2000)/Film A (2000).mkv" --apply --plan-from /config/plans/film-a.jsonl --background
```

- `--background` needs `--apply` and `--plan-from`. A backfill with it takes no `--convert`, `--sub-check`, scan,
  `--only-undecided` or `--plan-out`. `--check --plan-out FILE` writes the plan file of a check, and `--check` takes
  `--plan-from` only with `--background`.
- Each plan line holds the file key of the file that the dry run read: its inode, its size and its modification time. A
  file whose key differs does not queue. An apply changes the file, so a second run queues only the files left. A plan
  file of 2.18.6 or older holds no key. Run the dry run again.
- `--canary N` takes N of the files left, across the plan groups. `--limit N` queues N files.
- The worker runs each job as its dry run ran: a backfill's job as the flag backfill, and a job of `--check` as `--check
  --apply`. It plans the file again in a dry run first. It refuses the file when the file key differs, or when that
  plan differs from the plan line. The compare leaves out the label, the path and the file key, which the key check
  covers. Each write of the apply goes through one write gate, which compares it with the plan line first: the
  conversion, the header repair, the flag edits, the edits after a remux, the subtitle remux, the sidecar changes and
  the move back of the extras that the job's own conversion hid.
  The language hearing and the time limits can give the apply another answer. A guard in the worker fails any write on
  the file or its sidecars that skips the gate, and the job's line is then an error. A refusal writes nothing more and
  posts nothing. Its decision line has the outcome `plan_refused`, a result that says
  why, and `plan_diff`, both values of each field that differs. A new dry run plans the file again.
- The plan line holds the flag edits, a digest of the header repair with its subtitle trims and removals, and a digest
  of a conversion with the verdicts of its subtitle check and the name, language, flags, size and time of each sidecar
  it muxes. The plan of `--check` also holds the
  subtitle remux and the sidecar changes, by track and by sidecar, with a digest of their new times. So a job whose fit
  finds 4 s refuses a plan that retimes by 2 s.
- No dry run sees the file that a conversion makes. So after the job's own conversion, the rest of the job runs as
  `--check --apply` runs it, and the new file gets the flag edits, the header repair and the subtitle fixes it needs. A
  stop after the conversion, or after a later write of the job, leaves the key of the file in the job. Its next run goes
  on from that file, and its decision line says "this job converted the file before a stop" in `note`. A stop after
  another write of the job's own, as a header repair, ends with a refusal on the next run. Its result says "this job
  wrote the file before a stop" and names the writes.
- The jobs run `BACKGROUND_WORKERS` at a time, behind the imports, as the jobs of a bare backfill do. They count as jobs
  from a backfill, and `--backfill --drop-queued` drops them. The decision lines of a backfill's job have source
  `backfill_job`, and those of a job of `--check` have source `check_job`. `--queue` shows a running job by that source.
- A file that has a job waiting or running keeps that job. `--check` names such a file, and a path that no app lists,
  and then exits 1.
- A job gives way to an import that waits, before a conversion and before a remux. A job of `--check` keeps the
  original of a conversion for `KEEP_ORIGINALS_DAYS`, as `--check --apply` does. A backfill's job queues no other job,
  and asks the app to rescan the item after a header repair.
- A job of `--check` queues the burned-in subtitle check, the whole-file check and the deep analysis, as a job of a
  bare backfill does. At `BURNED_IN=fix` the burned-in subtitle check can turn subtitles off, and at `SUBTITLES=deep`
  the deep analysis can remux the file. So the plan line of `--check` holds the plans that its dry run made for both,
  and each job follows its plan. It compares before its write and refuses on any difference, as the job of the plan
  does. A plan of none refuses every write. The dry run plans the burned-in subtitle check on the flags that its own
  edits leave, as the job runs after them. The whole-file check of such a job writes nothing, because its re-grab only
  says what an import would do. After the job's own conversion these jobs run by their own settings, as after an
  import.
- A subtitle remux of the job of `--check` can change what its deep analysis finds. A deep analysis that then writes
  nothing ends with no change. One that writes otherwise refuses, and a new dry run of `--check` plans the rest.

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
arr-media-guard --audit radarr --since 24h --post             # the open problems of the last day, to Discord
```

- `--ids` takes the app's own ids: Radarr's movie id or Sonarr's series id, the `id` field of `/api/v3/movie` or
  `/api/v3/series`. A TMDB, TVDB or IMDb id does not work. It needs an instance.
- `--paths` limits the backfill, `--convert` and `--sub-check` to the listed files. It needs an instance. A scan
  refuses it.
- `--limit N` stops after N files that need a change. A scan checks N files, and a queue takes the next N files of
  each instance. A dry run with more than one worker starts no file after the stop. The files it already reads end, and
  the summary and the plan count them. So they can hold more than N files that need a change.
- The flag backfill and `--sub-check` print one line for each file with something to show. The line starts with the
  result. `dry run` says that `--apply` would write the file or a sidecar, with a flag edit, a header repair, a subtitle
  remux or a sidecar fix. `no change` says that it would write nothing. An apply names its change, as `edited`,
  `header repaired` or `subtitles remuxed: s2 retimed`. `header not repaired:` and `header repair skipped` give the
  reason first, then the issue. A remux carries the flag edits of its file, and the line still says `edited`. Bytes
  past the Matroska Segment end and each fault of the RFC 9559 check are a header issue, so `--apply` repairs them, see
  [RFC 9559 check](design.md#rfc-9559-check). When that remux must not run and the file has bytes past its Segment
  end, the line starts `not edited, as a flag edit would put`, and the flags stay.
- The first line counts the files the run reads, as `1488 files named .mkv`. When the run leaves out files not named
  `.mkv`, the line counts them too, and `--convert` takes them. A run with `--convert`, `--plan-from` or `--paths` gives
  no such count. The last line, `summary:`, counts each file of the run once, by one of these keys:
  - the outcome of its decision line, as `no_change`, `dry_run`, `edited`, `undecided` or `would_repack`
  - `not_selected`, a file the backfill leaves out, see above
  - `subtitles_cached`, a file whose saved subtitle check still holds, with `--sub-check`

  The line `of the files in the summary:` follows when some of those files also got a remux, or a dry run plans one.
  It counts `would_repair_header`, `header_repaired`, `repacked` and the codes of a subtitle remux, as `subtitle_retimed`
  or `would_remux_subtitles`.
- `--only-undecided` with `--plan-from` takes only the files the plan left undecided, for language detection.
- `--only-header` with `--plan-from` takes only the files the plan gives a header repair. It leaves a file whose only
  fault is padding, up to 1 MiB of bytes past the Segment end (`"padding_only": true` in the plan). This is the
  library pass of the RFC 9559 check, see [RFC 9559 check](design.md#rfc-9559-check). Each file still gets its whole
  plan, flag edits and padding too. `--canary N` and `--paths` narrow it. It does not go with `--only-undecided`.
  `--plan-from` names the plan file that a dry `--plan-out` run wrote, as in [Scans](#scans).
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
- An import queues the full burned-in subtitle check, the whole-file check and the deep analysis to run later.
  `--check` runs them right after the other checks, in the same way, and prints a line for each. They follow the same
  rules for a dry run and for `--apply`. They never wait for an import. The whole-file check and the deep analysis have
  no time limit, so a film can take many minutes.
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
  the app rescans the item. A variable frame rate posts no alert here. The decision line keeps it in `video.frame_rate`.
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
starts one. `--check-video` also runs the header check with its RFC 9559 check, and lists each header issue as
`HEADER`. A file whose only fault is padding gets no list line. The list is for a person to read. The scan writes it
again at the end of each run, and no other command reads it.

To repair the header issues, plan them in a dry run, then apply that plan with `--only-header`.

```
arr-media-guard --backfill radarr --plan-out /config/plans/radarr-header.jsonl                         # dry run, a new plan file
arr-media-guard --backfill radarr --apply --plan-from /config/plans/radarr-header.jsonl --only-header  # the header repairs
```

The dry run reads every `.mkv` file the app lists, unless `--ids` or `--paths` names some. It changes no media file.
Name a new plan file outside `STATE_DIR`. The dry run empties the file first, and AMG owns the files in `STATE_DIR`.
See [RFC 9559 check](design.md#rfc-9559-check).

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
- The decision line names the refusal in `repack.forced`.
- The option needs `--convert --apply` and `KEEP_ORIGINALS_DAYS` above 0.
- A path that is not in the run's list of files is reported and skipped.

A listed Sonarr file also skips the check that Sonarr reads the new file name as the same episodes. Scene numbering or
an alias can make that check wrong while the names are right. The conversion names the episodes by their ids, so the
link stays right. This needs no logged refusal.

- The extras, such as subtitles and `.nfo` files, keep that check. AMG takes them from the whole series folder. An extra
  whose name reads as other episodes still refuses the file and is named, so you can fix or remove it.
- The stream check still runs, and its refusal is forced only as above.
- The decision line names what Sonarr read in `repack.forced_name`, and the original stays in `.<NAME>-originals`.

## The audit

`--audit` reviews changes. With `--plan-from` it groups the plans of a dry run by the rule that made them. With
`--since` it lists the files that imports, backfills and `--check` with `--apply` left with a problem in that time. A
file that no app lists counts in no audit. AMG keeps the decision lines for 14 days. `--source hook` takes only the
imports, `--source backfill` only the backfills and the jobs they queued, and `--source check` only `--check` and
its jobs of `--background`. `--post` sends the list to
Discord, titled "Open problems: 2 files", when a file has an open problem.

The list has one line per file, with its problems in plain words. Each file counts once, by its latest import, check or
backfill. An error counts until a later run of the same check ends otherwise. The list leaves out a file that is gone,
and a failed conversion or file repair that already had its own alert. See "Audit" in [design.md](design.md#audit).
`--since` also counts the files with each RFC 9559 fault kind, by the latest header check of each file in that time,
scans and dry runs included. It prints the counts as `RFC 9559 faults: padding 2, version 1`, and the syslog line holds
them as `rfc_padding=2 rfc_version=1`.

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

After the policy file, the selftest names the version of the mkvmerge on PATH, as `mkvmerge version: 102.0`. AMG
supports mkvmerge 102, and the image ships it. An older mkvmerge, or one whose version does not read, gives one
warning that points to [Install on a host](../README.md#install-on-a-host). The warning never fails the selftest.

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

## The queue

`--queue` shows what the worker does now. It changes no record, so it is safe to run while the worker runs. Run it
where the worker runs, with `docker exec` in Docker. A one-off container sees no process of the service. A state store
that an older AMG wrote gets its upgrade first, as with every command.

A flag backfill, a backfill with `--plan-out` or `--plan-from`, runs in its own process and puts no job in the
background queue, so `--queue` never lists it. So do `--convert` and `--sub-check`. With `--background` the files of a
plan go to the background queue, see [Apply a plan in the background queue](#apply-a-plan-in-the-background-queue).
Such a run prints its lines in its own terminal. Its decision lines have source `backfill` in the decision log, see
[monitoring.md](monitoring.md), and `--audit <instance> --since 24h --source backfill` lists the files its `--apply`
left with an open problem. The jobs of `--background` have source `backfill_job`, which `--source backfill` takes too.

```
docker exec -it arr-media-guard arr-media-guard --queue
```

On a host, leave out `docker exec -it arr-media-guard`.

```
2026-10-18 09:30:00 EDT
worker: running
background queue: 12 jobs, 9 of them from a backfill (1 file still to check)
imports waiting: 1, 2 more wait to retry
import workers: 1 of 2 busy
  hook          started 09:29:58, 0:00:02  /movies/Film (2001)/Film (2001).mkv
background workers: 3 of 4 busy
  deep_analysis started 09:12:40, 0:17:20  /tv/Show/Season 1/Show - S01E01.mkv
  whole_file    started 09:29:02, 0:00:58  /movies/Film (2001)/Film (2001).mkv
  backfill_job  starting                   /movies/New (2010)/New (2010).mkv
process gone: 1
  backfill_job  process gone               /movies/Other (1999)/Other (1999).mkv
```

- `worker:` says whether a worker runs. `--queue` reads it from `/proc` and never tries the worker's lock. With no
  worker, the next import starts one on a host. In Docker the listener starts one within a minute when a job waits.
  `worker: unknown` means `--queue` could not read the open files of another user's arr-media-guard process. On a
  host, run it as root or as the app's user.
- The background queue line is the one of `--selftest`. `imports waiting` counts the imports that wait for a place,
  with the job files a busy store left in the queue folder. Then it counts the imports that wait to retry, as after an
  app that did not answer.
- The worker has one pool of places, the larger of `HOOK_WORKERS` and `BACKGROUND_WORKERS`. Each count is the busy
  places of its kind, against its own setting. Imports take up to `HOOK_WORKERS` places. Jobs of the background queue
  take up to `BACKGROUND_WORKERS`, only in places no import holds, and start only while no import waits. Such a job
  gives its place up to an import that waits. An import that finds every place held by jobs a backfill queued starts in
  one more process, see `BACKGROUND_WORKERS` in the [README](../README.md#settings). A count over its setting means the
  worker started before the setting changed.
- Each busy place has one line with the kind of its job, the time its process started, how long it has run, and its
  file. The kind is the `source` of the job's lines in the decision log. An import is `hook`. A job of the background
  queue is `burn_in`, `whole_file`, `deep_analysis`, `recheck`, `backfill_job` or `check_job`. A job the worker took and has not
  started yet says `starting`.
- A job whose process is gone stays marked as running, so no process runs it. The next worker puts it back in the
  queue. With no worker, each such job counts as gone. `claimed jobs that do not read` counts the ones whose record
  does not read.
- Two cases read wrong for a while. A job that just ended says `process gone` until the worker records its end. When
  the worker is killed while a job runs, that job keeps `worker: running` until it ends. A job that says `starting`
  then waits for the next worker.
- With `HOOK_WORKERS` and `BACKGROUND_WORKERS` at 1, the worker runs each job itself, and the store does not record
  which. `--queue` then says so under the worker line, `one place:`.
- With no state store yet, it says `background queue: 0 jobs`. When the store does not read, it prints the time and
  `the store did not read:` with the error, and exits 1.

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
| `arr-media-guard --queue` | none, it sees no process of the service | `$EXEC --queue` | |
| `arr-media-guard --test-discord` | `$RUN --test-discord` | `$EXEC --test-discord` | Discord |
| `arr-media-guard --backfill [sonarr] [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--restart]` | `$RUN --backfill ...` | `$EXEC --backfill ...` | API. The jobs need what the service needs. |
| `arr-media-guard --backfill [sonarr] --drop-queued` | `$RUN --backfill --drop-queued` | `$EXEC --backfill --drop-queued` | |
| `arr-media-guard --backfill sonarr --plan-out FILE [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--workers N]` | `$RUN --backfill sonarr --plan-out /config/plans/FILE ...` | `$EXEC --backfill sonarr --plan-out /config/plans/FILE ...` | API, Media, LID, Plex, Discord |
| `arr-media-guard --backfill sonarr --sub-check [--apply] [--recheck] [--ids ID ...] [--paths PATH ...]` | `$RUN --backfill sonarr --sub-check ...` | `$EXEC --backfill sonarr --sub-check ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --apply --plan-from FILE [--canary N]` | `$RUN --backfill sonarr --apply --plan-from /config/plans/FILE ...` | `$EXEC --backfill sonarr --apply --plan-from /config/plans/FILE ...` | API, Media, LID, Plex, Discord |
| `arr-media-guard --backfill sonarr --apply --plan-from FILE --background [--canary N] [--limit N]` | `$RUN --backfill sonarr --apply --plan-from /config/plans/FILE --background ...` | `$EXEC --backfill sonarr --apply --plan-from /config/plans/FILE --background ...` | API. The jobs need what the service needs. |
| `arr-media-guard --backfill sonarr --plan-from FILE --only-undecided [--apply] [--plan-out FILE]` | `$RUN --backfill sonarr --plan-from /config/plans/FILE --only-undecided ...` | `$EXEC --backfill sonarr --plan-from /config/plans/FILE --only-undecided ...` | API, Media, LID, Plex, Discord |
| `arr-media-guard --backfill sonarr --convert [--apply] [--plan-from FILE] [--canary N] [--workers N] [--plex-later]` | `$RUN --backfill sonarr --convert ...` | `$EXEC --backfill sonarr --convert ...` | API, Media, Plex |
| `arr-media-guard --backfill sonarr --convert --apply --force-convert PATH ...` | `$RUN --backfill sonarr --convert --apply --force-convert PATH ...` | `$EXEC --backfill sonarr --convert --apply --force-convert PATH ...` | API, Media, Plex |
| `arr-media-guard --plex-flush sonarr` | `$RUN --plex-flush sonarr` | `$EXEC --plex-flush sonarr` | Plex |
| `arr-media-guard --backfill sonarr --check-audio [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-audio ...` | `$EXEC --backfill sonarr --check-audio ...` | API, Media, Discord |
| `arr-media-guard --backfill sonarr --check-video [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-video ...` | `$EXEC --backfill sonarr --check-video ...` | API, Media, Discord |
| `arr-media-guard --sub-time PATH ... [--apply]` | `$RUN --sub-time /data/PATH ...` | `$EXEC --sub-time /data/PATH ...` | Media, LID. The API names the original language, and Plex gets the request after a change. It exits 0, 1 (with `--apply` an app lookup failed and nothing changed, or the policy file did not load), 2 (a wrong option), 3 (with `--apply` a planned change did not happen) or 4 (a path holds no file). |
| `arr-media-guard --check PATH ... --apply --plan-from FILE --background` | `$RUN --check /data/PATH ... --apply --plan-from /config/plans/FILE --background` | `$EXEC --check /data/PATH ... --apply --plan-from /config/plans/FILE --background` | API. The jobs need what the service needs. It exits 0, 1 (a file did not queue) or 2 (a wrong option). |
| `arr-media-guard --check PATH ... [--app sonarr] [--id ID \| --tmdb KIND/ID] [--apply] [--plan-out FILE]` | `$RUN --check /data/PATH ...` | `$EXEC --check /data/PATH ...` | Media, LID. The API names the item, and Plex and Discord get the requests of an apply. It exits 0, 1 (a path holds no file, another instance than `--app` lists it, a lookup failed, an item did not read, a check ended in an error, or the policy file did not load) or 2 (a wrong option, or `--id` or `--tmdb` with more than one path that no app lists). |
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
- `--sub-time --apply` stops the same way. The file in flight finishes with its decision line, and the next path never
  starts. A change the stop cut gives exit code 3. A dry run stops at once.
- After that stop, `--sub-time --apply` names each path it did not run as "not run, the run was stopped" and exits 3.
- A conversion, a header repair or a subtitle remux in progress with one worker stops, and the original stays as it
  was. The file still gets its decision line, which says the run was stopped.
- After a stop, the file in flight starts no new remux, conversion, flag edit or re-grab, and it runs no further check.
  Its decision line comes at once and lists the steps left out in `not_run`. A file stopped before its decision gets
  the outcome `stopped`. The next run of the file makes the rest.
- A stop posts no alert about itself. A re-grab that acted before the stop still posts. So does the "Unreadable
  subtitles" notice of a conversion that took out a track. With `DISCORD_POSTS=all`, the change posts of the writes that
  finished go out too. The next run of the file posts what it still finds.
- A command with no stop of its own, such as `--plex-flush`, ends at once. Its exit code is 143 after `docker stop` and
  130 after Ctrl+C.
- A running flag edit, re-grab, or the last step of a conversion always finishes first.
