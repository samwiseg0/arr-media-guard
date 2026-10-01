# arr-media-guard

A Custom Script connection for Sonarr and Radarr. It runs on every import and upgrade. It makes the right audio
track play first and sets the subtitle defaults to match. It re-grabs a file whose audio is broken or whose video is
corrupt, and posts one Discord alert for each problem it finds. When the apps run in Docker, the image takes a
Webhook connection instead. See [Run it in Docker](#run-it-in-docker).

Some release groups ship an English film with a Portuguese, Turkish or Hindi default track. Plex plays the default
track, so the viewer gets the wrong language. This hook fixes the flags in place with `mkvpropedit`. It never
re-encodes.

## What it does

- **Default tracks.** It classifies every audio and subtitle track once, then picks the audio and the subtitles from
  a policy file. When the signals conflict it abstains and edits nothing.
- **Language detection.** When a track's language is in doubt, an optional Whisper model hears the audio. A count
  of common words reads the language of subtitle text. A sidecar whose text is in another language than its name
  goes in with the text's language. A text track with a wrong tag gets a fixed tag, or an alert. When no audio track
  speaks its language, it also stops showing by itself. An `und` track takes the language its text reads.
- **Subtitle match.** Whisper hears two short windows of the audio, and the hook compares the words with each text
  subtitle and `.srt` sidecar in the audio's language. A subtitle of another episode leaves the file in a proven
  remux, or stays out of a conversion. A sidecar of another episode moves to the kept originals, and Bazarr can
  download it again. A subtitle that runs late by an offset or a frame-rate ratio gets new times. A text subtitle
  whose cues flash for a tenth of a second gets ends a viewer can read. `--sub-time` also times the subtitles it
  cannot hear, other languages and PGS or VobSub pictures, against a subtitle that matched the audio. An import does
  that too, as its time allows, and `SUBTITLES=deep` adds the rest in a deep analysis while no import waits.
- **Broken audio and corrupt video.** It samples the audio that will play and decodes three short video windows.
  A certain fault deletes the file, marks the grab failed and lets the app search again. A daily cap limits this.
- **Restore after a bad upgrade.** When the broken file was an upgrade, the old file comes back from the app's
  recycle bin, if it checks clean. Turn on the app's recycle bin, on the media's file system. `--selftest` and Test warn
  when it is off or elsewhere.
- **Wrong content.** It checks the file against TMDB and its own duration. It alerts, and it re-grabs only when you
  turn that on.
- **Header repair.** A lossless `mkvmerge` remux repairs a Matroska header with a wrong duration or a subtitle that runs
  past the end. The original stays for a week.
- **Conversion.** It can convert AVI, MP4, M4V, TS and WebM files into Matroska. It proves every stream of the new
  file packet by packet before the original goes. When the conversion of an import shows a damaged source, the hook
  re-grabs it like broken audio.
- **Backfill and scans.** The same script fixes the flags of your whole library, scans it for broken audio or corrupt
  video, and audits its own edits.

Every file it looks at gets one JSON line in the decision log. Every edit logs its undo command first.
[docs/design.md](docs/design.md) explains each rule and why it exists.

## Requirements

- Linux, with Sonarr and Radarr on the same host, or the Docker image. It is tested with Sonarr 4 and Radarr 6.
- Python 3.12 or later.
- `mkvtoolnix` and `ffmpeg` (for `mkvpropedit`, `mkvmerge`, `ffprobe` and `ffmpeg`).
  Tested with mkvtoolnix 92 and ffmpeg 7.1 (Debian 13). mkvtoolnix 82 reports no frame counts, so every header repair refuses.
- Optional: Plex, a Discord webhook, and a TMDB API read token.
- Optional: the language detection venv, about 450 MB, and its model, 464 MB. The pinned wheels are for CPython 3.13
  on x86_64 and aarch64.
- Known limits: the TMDB key fallback reads `/opt/Radarr`. In Docker, set `TMDB_TOKEN`, and mount each app's config
  folder at `RADARR_DIR` and `SONARR_DIR`.

## Install

```
sudo apt install mkvtoolnix ffmpeg python3-venv
sudo git clone https://github.com/samwiseg0/arr-media-guard /opt/arr-media-guard
sudo ln -s /opt/arr-media-guard/arr-media-guard /usr/local/bin/arr-media-guard
sudo install -m 0640 /opt/arr-media-guard/examples/arr-media-guard.env /etc/arr-media-guard.env
sudo install -m 0644 /opt/arr-media-guard/examples/policy.json /etc/arr-media-guard.policy.json
sudo mkdir -p /var/lib/arr-media-guard/alerts /var/lib/arr-media-guard/queue /var/lib/arr-media-guard/claimed
arr-media-guard --selftest
```

Install the policy file before the selftest. Without it the selftest fails and prints the command that creates it.

The script finds its modules in its own folder, symlinks resolved. Set `ARR_MEDIA_GUARD_LIB` to use another folder.
It reads `/etc/arr-media-guard.env`. Set `ARR_MEDIA_GUARD_ENV` to use another file.

Sonarr and Radarr run the script as their own user. That user must be able to write `STATE_DIR`, `LOG` and your media
files. Both apps share the state folder and its locks, so give them a shared group, or run both as the same user.

The decision log grows by one line per file. Rotate it weekly with logrotate. Keep `delaycompress` next to `compress`,
because a Plex folder scan reads the rest of `LOG.1` after a rotation. Leave out `copytruncate`, because the script
opens the log for each line.

The hook's children run in the app's cgroup. Add `OOMPolicy=continue` to the `[Service]` section of both app units,
so an OOM kill of a child never restarts the app. See "Memory" in the design notes.

### Language detection (optional)

```
sudo python3 -m venv /opt/arr-media-guard-lid/venv
sudo /opt/arr-media-guard-lid/venv/bin/pip install --require-hashes --only-binary=:all: \
    -r /opt/arr-media-guard/arr_lid.requirements.txt
sudo /opt/arr-media-guard-lid/venv/bin/python /opt/arr-media-guard/arr_lid.py --fetch \
    --model-dir /opt/arr-media-guard-lid/models
sudo touch /opt/arr-media-guard-lid/ready
```

`--fetch` downloads the pinned model once and checks its sha256. The hook uses detection only when `ready` exists.
Remove `ready` before you change the venv, and create it again after.

## The env file

`/etc/arr-media-guard.env` holds one `KEY='value'` per line. A comment needs its own line, because the value runs to
the end of the line. Every key is optional. [examples/arr-media-guard.env](examples/arr-media-guard.env) lists them all.

| Key | Default | What it does |
| --- | --- | --- |
| `LOG` | `/var/log/arr-media-guard.jsonl` | The decision log. |
| `STATE_DIR` | `/var/lib/arr-media-guard` | The queue, the locks, the caches, the scan lists and `status.json`. |
| `POLICY_FILE` | `/etc/arr-media-guard.policy.json` | The decision policy. Without it the hook edits nothing and alerts once. |
| `LID_DIR` | `/opt/arr-media-guard-lid` | The language detection venv and model. |
| `NAME` | `arr-media-guard` | The syslog tag and the name in alert footers. It also names two hidden folders. `.<NAME>-originals` at the top of each mount holds kept originals. `.<NAME>-convert` beside a video holds a remux's temp file, and the original and the extras during a conversion. Letters, digits, `.`, `_` and `-` only. |
| `INSTANCE` | the host name | The name of this host in logs and alerts. |
| `RADARR_URL`, `SONARR_URL` | `http://127.0.0.1:7878`, `http://127.0.0.1:8989` | The app APIs. |
| `RADARR_DIR`, `SONARR_DIR` | `/var/lib/radarr`, `/var/lib/sonarr` | The app's own folder. The script reads the API key from `config.xml` there. The conversion and the subtitle hunter read the app's database, `radarr.db` or `sonarr.db`, there too. |
| `RADARR_API_KEY`, `SONARR_API_KEY` | empty | The API key, for an app whose folder this host does not see. Empty: the key in `config.xml`. The conversion and the subtitle hunter still need the folder. |
| `PLEX_URL`, `PLEX_TOKEN` | empty | Plex re-analyzes an edited item. Plex must see the files under the same paths as the apps. An empty `PLEX_URL` turns every Plex call off. |
| `DISCORD_WEBHOOK` | empty | Alerts and scan summaries. Empty: nothing is posted. |
| `TMDB_TOKEN` | empty | A TMDB API read token. Empty: Radarr's bundled key, read from `/opt/Radarr/Radarr.Common.dll`. |
| `REGRAB` | `audio,video` | The kinds of fault that re-grab: `audio` (broken audio), `video` (corrupt video), `content` (wrong content) and `damage` (a damaged source that a conversion shows). A kind not listed gets the second check and only alerts "would re-grab". `none` turns every re-grab off. An unknown kind is left out, and a blank value re-grabs nothing. `--selftest` fails on both. |
| `REGRAB_CAP` | `30` | Re-grabs per app in 24 hours, then it alerts only. Every kind shares the one count. `0` turns every re-grab off. |
| `RESTORE` | `true` | A re-grab of a broken upgrade puts back the old file from the recycle bin. |
| `HEADER_REPAIR` | `true` | Remux a Matroska file whose header is wrong. `false`: log it only. |
| `SUBTITLES` | `fix` | What the subtitle check of an import does. Needs language detection. `off`: no check. `check`: it reads, reports and alerts, and changes nothing. `fix`: it also removes a wrong track, moves a wrong sidecar, retimes and lengthens flash cues. `deep`: `fix`, then a deep analysis after the import, which reads the whole file for unindexed subtitle tracks, hears one window a minute and times the subtitles that had no reference. It runs only while no import waits. `--sub-check` and `--sub-time` ignore it. Without `--apply` they report, and with it they fix. An unknown level acts as `check`, and `--selftest` fails on it. |
| `KEEP_ORIGINALS_DAYS` | `7` | Days a repair keeps the file it replaced in `.<NAME>-originals`, as a hard link, or as a verified copy where the file system refuses a link. `0` keeps nothing. |
| `REPACK_MAX_GB` | `30` | A larger file is never remuxed or converted. |
| `CONVERT` | `false` | Convert every imported file that is not Matroska. A backfill converts only with `--convert`. |
| `CONVERT_MAX_FILES` | `200` | Conversions one `--convert --apply` run makes. |
| `CONVERT_WORKERS` | `1` | Conversions a `--convert --apply` run makes at a time. |
| `SCAN_WORKERS` | `1` | Files a library scan or a dry-run backfill reads at a time. |
| `HOOK_WORKERS` | `1` | Import jobs the hook runs at a time. A season pack still runs at most this many. |

## The policy file

The decision reads its rules from `POLICY_FILE`, JSON. A missing key is an error, never a silent default.
[examples/policy.json](examples/policy.json):

```json
{
  "kids": {
    "genres": {"radarr": ["Family"], "sonarr": ["Children", "Family"]},
    "profiles": ["Kids"],
    "studios": ["Studio A"]
  },
  "audio": {
    "english": ["original"],
    "foreign": ["original", "english"],
    "foreign_kids": ["english", "original"]
  },
  "subtitles": {
    "english": ["forced"],
    "foreign": ["full", "sdh", "forced", "dub"]
  },
  "sparse_events": 1.5,
  "forced_flag_events": 4.0,
  "density_min_minutes": 15,
  "min_confidence": 0.7,
  "forced_clear": {"events": 10.0, "english_only_audio": true, "reference_ratio": 0.8}
}
```

| Key | What it does |
| --- | --- |
| `kids` | What makes a foreign title a kids title: an app genre, a Radarr quality profile name, or a studio. |
| `audio` | Per item class, the audio to try in order. `original` is the app's original language. |
| `subtitles` | Which English subtitle may play, by the audio that plays. `english` lists the roles allowed under English audio, normally `forced` only. `foreign` ranks the roles for other audio, and the file's best match turns on. The roles are `full`, `sdh`, `forced` and `dub`. |
| `sparse_events` | An English subtitle under this many events a minute counts as forced. |
| `forced_flag_events` | A forced flag counts only under this many events a minute. |
| `density_min_minutes` | A shorter file gives no reliable events a minute. |
| `min_confidence` | The file decides the language only at this confidence. A tag alone is 0.6. |
| `forced_clear` | A forced English subtitle that holds the full dialogue loses its forced flag under English audio. |

`--selftest` checks the rules against a fixed copy of the example policy, so your changes never fail it.

## Add it to Sonarr and Radarr

In each app, open Settings, Connect, add a **Custom Script**, and set:

- Name: `arr-media-guard`
- Triggers: **On File Import** and **On File Upgrade**
- Path: `/usr/local/bin/arr-media-guard`
- Arguments: empty

Save runs the Test event. The script answers `arr-media-guard: Test ok`. On an import it queues a job and returns at
once. A worker process does the checks and the edit.

## Dry runs and the backfill

A backfill is dry unless you add `--apply`. It runs at nice 19 and idle I/O priority, takes the hook's file lock, and
never posts to Discord. Run a large one in steps, and note the time before each apply:

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
arr-media-guard --backfill sonarr --check-audio --limit 2000  # scan for broken audio, resumes where it stopped
arr-media-guard --backfill sonarr --check-video --restart     # scan for corrupt video, a new pass
arr-media-guard --backfill sonarr --convert                   # the files that are not .mkv, dry
arr-media-guard --backfill sonarr --convert --apply           # convert them, CONVERT_MAX_FILES a run
arr-media-guard --backfill sonarr --ids 101 --sub-check       # check the subtitles of one series against the audio, dry
arr-media-guard --backfill radarr --sub-check --apply --paths "/data/movies/Film A (2000)/Film A (2000).mkv"
arr-media-guard --sub-time "/data/movies/Film A (2000)/Film A (2000).mkv"   # time every subtitle of one file, dry
arr-media-guard --subhunt radarr --ids 123                    # find a release with English subtitles, dry. Needs SABnzbd and NZBHydra2.
arr-media-guard --audit radarr --since 24h --post             # the hook's edits of the last day, to Discord
```

An apply asks Plex to analyze each edited item, after the section is idle on two checks. When "Scan my library
automatically" is off in Plex, the analyzes that follow need one idle check each. See docs/design.md, "The burst".

`--sub-check` adds the subtitle match check to a backfill, which never runs it by default. It also takes a file with
a `.srt` sidecar beside it. A dry run prints each verdict and the removal or timing fix it would make, and an apply
acts as an import does. `--paths` limits the flag backfill, `--convert` and `--sub-check` to the listed files, and
a scan refuses it. The check caches its verdicts, so a later
run skips a file that stays as it was, with the same sidecars, and needs nothing more. A stopped run goes on where it
stopped. It hears at the backfill's priority with one Whisper thread. The last line
counts the files checked, the verdicts, the timing fixes and the CPU time, and estimates the time for the whole
library.

### Time the subtitles of one file

```
arr-media-guard --sub-time "/data/tv/Show A/Season 1/Show A - S01E02.mkv"           # dry run: changes nothing
arr-media-guard --sub-time "/data/tv/Show A/Season 1/Show A - S01E02.mkv" --apply   # makes the changes
```

`--sub-time` runs the subtitle check of `--sub-check` on each file it names, and reads the cache of neither. It then
times every other subtitle against a track or sidecar whose words matched the audio, and hears one window a minute. The
file needs no app. For a path outside Sonarr and Radarr, such as a copy, it runs with no item, so it knows no original
language. A mismatch then counts unless the file also holds audio in another language. With `--apply` it then keeps
every audio and subtitle flag. Only a subtitle that does not match the audio loses its default and forced flags. When
an app lookup fails, `--apply` stops before any change and exits 1. A planned change can also fail to happen. Examples
are a remux or a header repair that failed or was skipped, a flag edit that failed, and a sidecar left as it was.
`--apply` then lists each such file and exits 3. A path that holds no file is skipped, and the run then exits 4.
Otherwise it exits 0.

A subtitle track that old mkvmerge versions left out of the Cues index is read from the whole file. That read keeps
only the cue times and text as ffmpeg streams them, so it writes no file.

Each subtitle gets one line. The columns are:

- the track's place, such as `s3`, or the sidecar's name
- the codec, the language and the role (`full`, `sdh` or `dub`)
- the method. `words` is the check against the heard audio, with `a reference: in time`, `fixed` or `clean sweep` when
  it times other tracks. `reference s1` means the track was timed against `s1`. `reference, none` means no track
  could time it.
- the verdict. The word check gives `match`, `mismatch` or `unknown`. The reference timing gives `fit` or `weak` with
  its score, where a weak fit only reports, or `unknown` or `deferred`.
- the new times, as an offset and a frame-rate ratio, or `in time`
- the action, such as `none`, `retimed`, `removed`, `flags off`, `report only` or the remux it would make
- why

A `flash` line names a text track whose cues show for a tenth of a second, and the new ends of its first cues. The sweep
table has one row per heard window: its time, the heard words, the share that matched the cues, the cues whose first
word matched, and their offset. `ALERT` marks two neighbouring windows 1 second or more off the fitted line the same
way. One such window alone is marked "one window alone" and does not alert. The decision log holds the
results under `subcheck`, `subtime`, `flash` and `sweep`.

With `--apply`, a built-in track gets its new times, new ends or its removal in one remux that the packet proof checks.
A sidecar is written again or moved. The original file or sidecar is kept for `KEEP_ORIGINALS_DAYS` (7) days at
`<mount>/.<NAME>-originals/<UTC time>/<path from the mount>`, where the mount holds the file. To undo a change, move the kept
original back over the file with `mv`, then rescan the item in the app.

Scans never delete or re-grab. They list what they find in `STATE_DIR`. Schedule the `--audit ... --since 24h --post`
line nightly with a systemd timer or cron to review the hook's own edits.

### Force a conversion

The proof refuses a file that it cannot prove lossless, and the decision log holds the refusal. When you checked the
refusal and accept it, name the file:

```
arr-media-guard --backfill radarr --convert --apply --force-convert "/data/movies/Film A (2000)/Film A (2000).mp4"
```

The run then takes only the listed files. A file converts when the proof refuses it for the same reason as the last
refusal in the decision log. Another refusal, or no logged refusal, is not forced, and the run says why. Every other
step runs as normal. The original stays in `.<NAME>-originals` for `KEEP_ORIGINALS_DAYS`, so one move puts it back. The
decision line names the refusal in `repack.forced`, and the nightly audit says the conversion was forced. The option
needs `--convert --apply` and `KEEP_ORIGINALS_DAYS` above 0. A path that is not in the run's work list is
reported and skipped.

A listed Sonarr file also skips the check that Sonarr reads its new video name as its own episodes. Scene numbering or an
alias can make that check wrong while the names are right. The conversion names the episodes by id, so the link stays
right. This needs no logged refusal. The extras beside the video keep the check, and an extra whose name maps to other
episodes still refuses the file and is named, so you can fix or remove it. The proof still runs, and a proof refusal is
forced only as above. The decision
line names the parse result in `repack.forced_name`, the original stays in `.<NAME>-originals`, and the audit says "name
forced".

## The decision log

`LOG` gets one JSON line per file that a run looks at: the hook, every backfill, the scans and the audit. A line holds
the app and item ids, every track with its language, confidence and role, the plan, the reason codes, the alerts and
an `outcome` code. The same summary goes to syslog as logfmt, tagged `NAME`.

Before each edit the hook writes an `editing` line whose `undo` field is the full `mkvpropedit` command that restores
the old flags. To reverse the last edit of a film:

```
python3 - <<'PY'
import json, subprocess
recs = [json.loads(line) for line in open("/var/log/arr-media-guard.jsonl")]
last = [r for r in recs if r.get("result") == "editing" and r["label"] == "Film A (2000)"][-1]
subprocess.run(last["undo"], check=True)
PY
```

`STATE_DIR/status.json` records the TMDB and policy checks for a monitoring agent. See "Status file" in the design
notes.

## Run it in Docker

The image holds the script, `ffmpeg`, `mkvtoolnix`, the language detection venv and the pinned Whisper model. The build
checks the model's sha256, and nothing downloads at run time. Sonarr and Radarr reach it through a **Webhook**
connection, so nothing goes into their containers. The published image is for amd64. An arm64 image is not published
yet.

```yaml
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    environment: [PUID=1000, PGID=1000, TZ=Etc/UTC]
    volumes: [./sonarr:/config, /srv/media:/data]
    ports: ["8989:8989"]
  radarr:
    image: lscr.io/linuxserver/radarr:latest
    environment: [PUID=1000, PGID=1000, TZ=Etc/UTC]
    volumes: [./radarr:/config, /srv/media:/data]
    ports: ["7878:7878"]
  arr-media-guard:
    image: ghcr.io/samwiseg0/arr-media-guard:1.6.0
    container_name: arr-media-guard
    environment: [PUID=1000, PGID=1000, TZ=Etc/UTC]   # TZ: your time zone, for AUDIT_TIME and the log times
    volumes:
      - ./arr-media-guard:/config
      - ./sonarr:/var/lib/sonarr:ro
      - ./radarr:/var/lib/radarr:ro
      - /srv/media:/data
    stop_grace_period: 1m
    restart: unless-stopped
```

1. Run `docker compose up -d`. The first start writes `arr-media-guard.env` and `policy.json` into `/config`. The
   listener exits until the env file has a Webhook user and password.
2. Set `WEBHOOK_USER` and `WEBHOOK_PASSWORD` in `./arr-media-guard/arr-media-guard.env`. Set `TMDB_TOKEN` too,
   because the image has no Radarr install to read the bundled key from. Then run `docker compose up -d` again.
3. In each app, open Settings, Connect, add a **Webhook**, and set:
   - Name: `arr-media-guard`
   - Triggers: **On File Import** and **On File Upgrade** only
   - URL: `http://arr-media-guard:8484/sonarr` in Sonarr, `http://arr-media-guard:8484/radarr` in Radarr
   - Method: `POST`
   - Username and Password: `WEBHOOK_USER` and `WEBHOOK_PASSWORD`

   Test checks the policy, the API key and each root folder of the app, and Save runs it. A failed Test names what
   to fix. Turn on the app's recycle bin, on the media's file system, so a bad upgrade can be undone. Test warns in
   the log when it is off or elsewhere.

| Mount | What it holds |
| --- | --- |
| `/config` | The env file, `policy.json`, the state folder `state/` and the decision log in `logs/`. |
| `/var/lib/sonarr`, `/var/lib/radarr` | The app's config folder, read-only. The script reads the API key from `config.xml`, and a conversion reads the app's database. SQLite reads a live WAL database through a read-only mount, also from another container. |
| The media | At the same path as in the apps, `/data` above. Else set `PATH_MAP`. |

The Docker section at the end of the env file sets the paths under `/config` and these keys. Every other key in
[The env file](#the-env-file) works the same way.

| Key | Default | What it does |
| --- | --- | --- |
| `WEBHOOK_USER`, `WEBHOOK_PASSWORD` | empty | The basic auth of the Webhook connection. Printable ASCII. Without both, the listener does not start. |
| `RADARR_URL`, `SONARR_URL` | `http://radarr:7878`, `http://sonarr:8989` | The apps as this container reaches them. |
| `PATH_MAP` | empty | `APP_PATH:LOCAL_PATH` pairs joined by `\|`, for example `/data:/media`, when this container sees the media at other paths. Plex must see the files under the apps' paths. A pair that is not two absolute paths fails `--selftest`, and the listener does not start. |
| `AUDIT_TIME` | `07:30` | The local time of the nightly audit of each app, which also removes kept originals older than `KEEP_ORIGINALS_DAYS`, and of the weekly log rotation. Empty: neither runs. |
| `INSTANCE` | `arr-media-guard` | The name of this install in logs and alerts. The host name of a container changes with each new container. |

Set `TZ` on the container to your time zone, for example `TZ=Europe/Berlin`. Without it the container runs in UTC, so
`AUDIT_TIME` and the times in the decision log are UTC.

The listener takes each post only with the right user and password, and a body of at most 1 MiB. It asks the app
for the file by its id and uses the path the API gives. It refuses a file of another item, and an old file outside
the item's folder or the recycle bin, with a line in the decision log. Each request has 10 seconds in all, and 32 run
at once, so a slow or idle client never holds up an app. 64 more connections may wait for a thread, and the listener
closes each new one past those 96 at once. A post with a wrong path or wrong credentials never reaches the decision
log. The listener counts them and prints one summary line a minute at most. The compose example publishes no port of
arr-media-guard on purpose: only the apps on the compose network reach it. The worker runs in the same container, and
the listener starts it again when a job waits. The script runs as `PUID:PGID`, so the files it writes keep the owner
of the media. A stop waits for a running flag edit. The healthcheck asks `http://127.0.0.1:8484/health`.

### Commands in Docker

Every command of a native install runs in the image, as a one-off container or with `docker exec`. Both run as
`PUID:PGID`, and the exit code of the command is the exit code of `docker run` and `docker exec`. A one-off container
shares `/config` with the service, so it takes the same file lock and keeps its state beside the service's. Run a long
command, such as a backfill, a conversion or a scan, as a one-off container. A restart of the service, as an image
update does, ends every `docker exec` process in it. `docker compose run --rm arr-media-guard ARGS` gives the same
one-off container as `$RUN ARGS` below, with the mounts of the compose file.

```
RUN="docker run --rm --network media_default -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -v $PWD/arr-media-guard:/config -v $PWD/sonarr:/var/lib/sonarr:ro -v $PWD/radarr:/var/lib/radarr:ro \
  -v /srv/media:/data ghcr.io/samwiseg0/arr-media-guard:1.6.0"
EXEC="docker exec -it arr-media-guard arr-media-guard"
```

`media_default` is the compose network, named after the folder of the compose file. Keep `-it` on `docker exec`, so
Ctrl+C reaches the command, and leave it out in a script. Each command needs `/config`.
The last column names what else it needs:

- **API**: the app's URL, and its key from the config folder mount or `<APP>_API_KEY`.
- **DB**: the app's config folder at `RADARR_DIR` or `SONARR_DIR`, `/var/lib/<app>` by default, for its database.
- **Media**: the media at the apps' paths, or `PATH_MAP`. An apply writes there.
- **LID**: language detection, which the image holds.
- **Plex** and **Discord**: `PLEX_URL` and `PLEX_TOKEN`, and `DISCORD_WEBHOOK`. Without them the command skips the
  analyze or the post.

| Native command | One-off container | `docker exec` | Needs |
| --- | --- | --- | --- |
| `arr-media-guard` | none, the Webhook listener takes its place | none | |
| `arr-media-guard --serve` | the service itself | none | API, DB, Media, LID, Plex, Discord |
| `arr-media-guard --selftest` | `$RUN --selftest` | `$EXEC --selftest` | |
| `arr-media-guard --backfill sonarr [--apply] [--ids ID ...] [--paths PATH ...] [--limit N] [--plan-out FILE] [--workers N]` | `$RUN --backfill sonarr ...` | `$EXEC --backfill sonarr ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --sub-check [--apply] [--ids ID ...] [--paths PATH ...]` | `$RUN --backfill sonarr --sub-check ...` | `$EXEC --backfill sonarr --sub-check ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --apply --plan-from FILE [--canary N]` | `$RUN --backfill sonarr --apply --plan-from /config/FILE ...` | `$EXEC --backfill sonarr --apply --plan-from /config/FILE ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --plan-from FILE --only-undecided [--apply] [--plan-out FILE]` | `$RUN --backfill sonarr --plan-from /config/FILE --only-undecided ...` | `$EXEC --backfill sonarr --plan-from /config/FILE --only-undecided ...` | API, Media, LID, Plex |
| `arr-media-guard --backfill sonarr --convert [--apply] [--plan-from FILE] [--canary N] [--workers N] [--plex-later]` | `$RUN --backfill sonarr --convert ...` | `$EXEC --backfill sonarr --convert ...` | API, DB, Media, Plex |
| `arr-media-guard --backfill sonarr --convert --apply --force-convert PATH ...` | `$RUN --backfill sonarr --convert --apply --force-convert PATH ...` | `$EXEC --backfill sonarr --convert --apply --force-convert PATH ...` | API, DB, Media, Plex |
| `arr-media-guard --plex-flush sonarr` | `$RUN --plex-flush sonarr` | `$EXEC --plex-flush sonarr` | Plex |
| `arr-media-guard --backfill sonarr --check-audio [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-audio ...` | `$EXEC --backfill sonarr --check-audio ...` | API, Media, Discord |
| `arr-media-guard --backfill sonarr --check-video [--ids ID ...] [--limit N] [--restart] [--workers N]` | `$RUN --backfill sonarr --check-video ...` | `$EXEC --backfill sonarr --check-video ...` | API, Media, Discord |
| `arr-media-guard --sub-time PATH ... [--apply]` | `$RUN --sub-time /data/PATH ...` | `$EXEC --sub-time /data/PATH ...` | Media, LID. The API names the original language, and Plex gets the analyze after an edit. It exits 0, 1 (an app lookup failed, nothing changed), 3 (a planned change did not happen) or 4 (a path holds no file). |
| `arr-media-guard --audit sonarr (--plan-from FILE \| --since 24h) [--source hook\|backfill] [--post]` | `$RUN --audit sonarr --since 24h ...` | `$EXEC --audit sonarr --since 24h ...` | API, Media, Discord |
| `arr-media-guard --subhunt radarr --ids ID ... [--apply] [--force]` | `$RUN --subhunt radarr --ids ID ...` | `$EXEC --subhunt radarr --ids ID ...` | API, DB, Media, LID, and SABnzbd and NZBHydra2 on the network. The download folder at SABnzbd's path. |

Every `sonarr` above takes `radarr` too, except `--subhunt`, which takes `radarr` only. A file a command names, such
as `--plan-out` or `--plan-from`, is a path in the container. Put it under `/config` to keep it. The audit needs Media
and the API to remove kept originals, and the decision log for the rest.

`tini` is PID 1 in the image. `docker stop` sends SIGTERM and Ctrl+C sends SIGINT to the command's process group, as
in a terminal. A scan or a backfill ends the files in flight and stops, and the next run resumes at its saved place. A
command with no stop of its own, such as `--plex-flush`, ends at once, with exit code 143 after `docker stop` and 130
after Ctrl+C. A running flag edit, re-grab or swap ends first.

With `SUBTITLES=deep`, an import queues a deep analysis of its file, and the worker in the service runs it while no
import waits. A deep analysis that a stopped container left in `/config/state/deep-analysis/` runs after the next start.
The listener starts a worker for it within a minute.

The listener reads the env file and the policy when it starts. Restart the container after you change either.

**Upgrade.** Set the new version in `image:`, then run `docker compose pull arr-media-guard` and
`docker compose up -d arr-media-guard`. `/config` keeps the state, the caches and the log. The container writes the
env file only on its first start, so add by hand the keys a new release names.

**From a native install.** Remove the Custom Script connection in each app and add the Webhook. Copy the keys you
changed from `/etc/arr-media-guard.env` into the Docker env file, and keep its paths. Keep `NAME`, so the audit still
finds the kept originals in `.<NAME>-originals`. To keep the caches and the re-grab counts, stop the container, copy the
old `STATE_DIR` into `./arr-media-guard/state`, and start it.

**Build it yourself.** `docker build -f docker/Dockerfile -t arr-media-guard .` in a clone.

## Try it on one file

`--sub-time` runs on one file with no Sonarr, no Radarr and no settings. Put the video in a folder, open a terminal in
that folder, and mount it at `/media`. The first command is a dry run. It prints one line for each subtitle, the flash
check and the sweep of heard windows, and it changes nothing. It prints what `--apply` would change. A line that starts
with `ALERT subtiming` and ends with "The file stays as it was" names such a change, and it is no error. The second
command makes the changes.

Linux and macOS:

```
docker run --rm -it -e PUID=$(id -u) -e PGID=$(id -g) -v "$PWD:/media" ghcr.io/samwiseg0/arr-media-guard:1.6.0 --sub-time "/media/Episode.mkv"
docker run --rm -it -e PUID=$(id -u) -e PGID=$(id -g) -v "$PWD:/media" ghcr.io/samwiseg0/arr-media-guard:1.6.0 --sub-time "/media/Episode.mkv" --apply
```

Windows PowerShell:

```
docker run --rm -it -v "${PWD}:/media" ghcr.io/samwiseg0/arr-media-guard:1.6.0 --sub-time "/media/Episode.mkv"
docker run --rm -it -v "${PWD}:/media" ghcr.io/samwiseg0/arr-media-guard:1.6.0 --sub-time "/media/Episode.mkv" --apply
```

The first lines say that Radarr and Sonarr do not run. That is expected. No app lists the file, so the run keeps every
flag, except that a subtitle whose words do not match the audio loses its default and forced flags. With `--apply`, a
remux that the packet proof checks writes the new times, the new ends or the removal. The original stays as a hard link
at `.arr-media-guard-originals/<UTC time>/Episode.mkv` in the mounted folder, and the output names that path. A later
`--apply` that keeps an original in the same folder removes the kept originals older than `KEEP_ORIGINALS_DAYS`, 7 days
by default. Move a kept original out of that folder to keep it longer. To undo the change, move it back over the file:

```
mv ".arr-media-guard-originals/<UTC time>/Episode.mkv" "Episode.mkv"                        # Linux and macOS
Move-Item -Force ".arr-media-guard-originals\<UTC time>\Episode.mkv" "Episode.mkv"          # Windows PowerShell
```

A folder that refuses hard links, as some Windows and network mounts do, gets a copy of the original instead. The copy is
checked against the original before the change, and the output says "copied" instead of "hard-linked". It needs the
file's size plus 1 GB free in that folder. With less, nothing changes, and the output says why.

The command exits 0 when all went well, and 4 when the path holds no file. With `--apply` it exits 3 when a planned
change did not happen, and 1 when an app lookup failed before any change.

The sweep hears one window a minute and takes a few minutes of CPU. A 24-minute episode took about a minute on the test
host. The run was tested on Linux. Windows with Docker Desktop, and Apple Silicon, which runs the amd64 image under
emulation, were not tested.

## Tests

```
python -m pytest -q
```

The tests need pytest. The tests on real media files need `ffmpeg` and `mkvtoolnix` and skip without them.

## License

GPL-3.0. See [LICENSE](LICENSE).
