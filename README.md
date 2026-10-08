# arr-media-guard

arr-media-guard (AMG) checks every file that Sonarr or Radarr imports or upgrades. It makes the right audio play first
and sets the subtitles to match. It fixes subtitles that are out of sync, and takes out subtitles of another episode. It
deletes a broken file so Sonarr or Radarr downloads another copy. When the broken file was an upgrade, it can put back
the file the upgrade replaced. AMG changes track flags in place with `mkvpropedit`, and never re-encodes the video or
the audio. It posts a Discord alert for each problem it leaves unresolved, and logs the problems it fixed.

In these docs, *the app* is the Sonarr or Radarr that sent the file, and the *item* is its movie or series.

- **Default tracks.** It picks the audio and the subtitles that play first, by the rules of a policy file.
- **Original language flag.** It marks the audio and subtitles in the language the film or show was made in, after it
  hears the audio or reads the subtitle text.
- **Language detection.** An optional speech model, Whisper, hears an audio track whose language is in doubt.
- **Subtitle match.** It finds a subtitle of another episode by its words, and retimes one that is early, late or
  drifting.
- **Whole-file timing.** The deep analysis after an import listens to the whole audio and times every subtitle line.
- **Subtitle block timing.** When only part of a subtitle is out of sync, it moves just those lines to their speech.
- **Mid-file jump timing.** When a subtitle jumps out of sync in the middle of a file, it moves the lines after the jump.
- **Live caption timing.** It moves each line of live captions to where it is spoken.
- **Foreign subtitle timing.** It retimes subtitles in a language no audio track speaks, by where people speak.
- **Incorrect subtitle identification.** It alerts on a foreign subtitle whose lines do not fit the speech.
- **Long subtitle track handling.** It reads tracks of up to 100,000 lines, and stops a read that takes too long.
- **Subtitle character set detection.** It reads a `.srt` file saved in an old character set, such as Big5.
- **Garbled subtitle repair.** It repairs subtitles an old muxer garbled, and takes out a track it cannot repair.
- **Broken files.** It deletes a file with broken audio or corrupt video, so Sonarr or Radarr searches again. It can put
  back the old file that the upgrade replaced.
- **Wrong content.** It alerts when a file holds another film or another episode.
- **Repairs.** It repairs a wrong Matroska header. It can convert other containers, such as AVI or MP4, into Matroska.
- **Backfill and scans.** It fixes and scans the files you already have, and audits its own edits.
- **Subtitle hunter.** A command of its own replaces a Radarr film that has no English subtitle with a release that
  has one.

[docs/features.md](docs/features.md) says what each check does and when it runs.

## Requirements

- Docker. The image holds Python, the tools, language detection and its model. It is for amd64. An arm64 image is not
  published yet.
- Sonarr 4 and Radarr 6. These are the versions it is tested with.
- Optional: Plex, a Discord webhook, and a TMDB API read token.

On a host, it needs these instead of Docker:

- Linux and Python 3.12 or later, with Sonarr and Radarr on the same host.
- `mkvtoolnix` and `ffmpeg`, for `mkvpropedit`, `mkvmerge`, `ffprobe` and `ffmpeg`. It is tested with mkvtoolnix 92
  and ffmpeg 7.1 (Debian 13). mkvtoolnix 82 reports no frame counts, so AMG refuses every header repair with it.
- Optional: the language detection venv, about 450 MB, and its model, 464 MB. It needs Python 3.13 on x86_64 or aarch64,
  because its pinned wheels are built for CPython 3.13.

## Install in Docker

Docker is the recommended way. The image works wherever Sonarr and Radarr run. AMG needs each app's API at a URL it
can reach. It needs the media at the paths the apps use, or through [path maps](docs/docker.md#path-maps). The apps
post to AMG's *listener*, a small web server in the container, through a **Webhook** connection. So nothing goes into
their containers. AMG never reads an app's database, so the apps may run on other hosts too.

1. Download [docker/compose.yml](docker/compose.yml) into a new folder:
   ```
   mkdir amg && cd amg
   curl -fsSLO https://raw.githubusercontent.com/samwiseg0/arr-media-guard/main/docker/compose.yml
   ```
   To run it in the compose file of your apps, copy its service and its volume there instead.
2. Replace each `CHANGE_ME` in the file. Each app needs its URL as this container reaches it, and its API key from
   Settings > General. The config folder is a folder of the host, such as `/srv/appdata/arr-media-guard`. The media
   folder of the host goes at the path the apps use. Set `PUID`, `PGID` and `TZ` to the apps' user and group and your
   time zone. To turn on an optional line, remove its `# ` (the hash and its space) and replace its `CHANGE_ME`.
3. Run `docker compose up -d`. The first start writes `arr-media-guard.env` and `policy.json` into the config folder.
   The listener generates a random Webhook password and writes it into that env file as `WEBHOOK_PASSWORD`.
4. Copy `WEBHOOK_USER` and `WEBHOOK_PASSWORD` from `arr-media-guard.env` in the config folder into the Username and
   Password of each app's Webhook connection, see [Add it to Sonarr and Radarr](#add-it-to-sonarr-and-radarr). The
   user is `arr-admin`, and every app takes the same pair. The file has mode 0640 and belongs to `PUID:PGID`. When
   your login user is not `PUID`, read it with `sudo cat`.

To run it without compose, replace each `CHANGE_ME` in this command as in step 2, and run it:

```
docker run -d --name arr-media-guard --restart unless-stopped --stop-timeout 60 -p 8484:8484 \
  -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -e RADARR_URL=http://CHANGE_ME:7878 -e RADARR_API_KEY=CHANGE_ME \
  -e SONARR_URL=http://CHANGE_ME:8989 -e SONARR_API_KEY=CHANGE_ME \
  -v CHANGE_ME:/config -v amg-state:/config/state --mount type=bind,source=CHANGE_ME,target=/data \
  ghcr.io/samwiseg0/arr-media-guard:latest
```

Then do step 4. AMG keeps its queue and its records in a small database, the *state store*. It needs a local disk, so
it gets the volume `amg-state` of its own. To send AMG's log lines to the host's syslog, for example for Loki, also
mount `/dev/log`.

[docs/docker.md](docs/docker.md) has a compose file with the apps, the mounts, path maps for other media paths,
secrets in a file of their own, and the listener.

## Install on a host

Use this way when Sonarr and Radarr run on the host. Each app runs the script as a **Custom Script** connection.

1. Install the tools, the code and the state folders:
   ```
   sudo apt install mkvtoolnix ffmpeg python3-venv
   sudo git clone https://github.com/samwiseg0/arr-media-guard /opt/arr-media-guard
   sudo ln -s /opt/arr-media-guard/arr-media-guard /usr/local/bin/arr-media-guard
   sudo ln -s /opt/arr-media-guard/arr-media-guard-subhunt /usr/local/bin/arr-media-guard-subhunt
   sudo mkdir -p /var/lib/arr-media-guard
   ```
2. Install the env file and the policy file. Without the policy file the selftest fails.
   ```
   sudo install -m 0640 /opt/arr-media-guard/examples/arr-media-guard.env /etc/arr-media-guard.env
   sudo install -m 0644 /opt/arr-media-guard/examples/policy.json /etc/arr-media-guard.policy.json
   ```
3. Give the app users access. Sonarr and Radarr run the script as their own user. That user must read the env file, and
   write `STATE_DIR`, `LOG` and your media files. Both apps share the state folder and its locks. So give them a shared
   group, or run both as the same user. Step 2 leaves the env file readable by root only, so give it that group:
   `sudo chgrp media /etc/arr-media-guard.env`, with your group's name. When the app user cannot read the file, the
   selftest and the app's Test fail and name the file and the user.
   [docs/regrabs.md](docs/regrabs.md#where-the-folders-go) says where AMG keeps its copies of old files.
4. Add `OOMPolicy=continue` to the `[Service]` section of both app units. AMG's checks run inside the app's service.
   This setting keeps the app running when one of those checks runs out of memory. See
   [Memory](docs/design.md#memory).
5. Rotate the *decision log* weekly with logrotate, and leave out `copytruncate`. The decision log is the file `LOG`
   names, with one line for each file AMG checks. [docs/monitoring.md](docs/monitoring.md#rotate-the-decision-log) has a
   ready file.
6. Schedule `arr-media-guard --audit radarr --since 24h --post` nightly, and the same for `sonarr`. Use a systemd
   timer or cron. The audit reviews the day's edits, and removes the files AMG kept that are older than
   `KEEP_ORIGINALS_DAYS`. It also turns on On Grab when `KEEP_REPLACED` needs it.
7. Run `arr-media-guard --selftest`.

AMG also reads its settings from the app's environment, and the environment wins over the env file. So a variable of the
app with a common name, such as `NAME`, `LOG`, `INSTANCE` or `STATE_DIR`, changes AMG's settings. At its start the
*worker* names each key it took from the environment in a warning line of the decision log. The worker is the AMG
process that takes the queued jobs and checks the files.

To turn on language detection, also run:

```
sudo python3 -m venv /opt/arr-media-guard-lid/venv
sudo /opt/arr-media-guard-lid/venv/bin/pip install --require-hashes --only-binary=:all: \
    -r /opt/arr-media-guard/arr_lid.requirements.txt
sudo /opt/arr-media-guard-lid/venv/bin/python /opt/arr-media-guard/arr_media_guard/lid.py --fetch \
    --model-dir /opt/arr-media-guard-lid/models
sudo touch /opt/arr-media-guard-lid/ready
```

`--fetch` downloads the pinned model once and checks its sha256. AMG uses language detection only when `ready`
exists. Remove `ready` before you change the venv, and create it again after.

## Add it to Sonarr and Radarr

In each app, open Settings, Connect, and add a connection. Use a **Webhook** in Docker and a **Custom Script** on a
host.

| Field | Webhook (Docker) | Custom Script (host) |
| --- | --- | --- |
| Name | `arr-media-guard` | `arr-media-guard` |
| Triggers | **On File Import** and **On File Upgrade** | **On File Import** and **On File Upgrade** |
| URL or Path | `http://<docker host>:8484/sonarr` in Sonarr, `http://<docker host>:8484/radarr` in Radarr. On one compose network, `http://arr-media-guard:8484/sonarr` and so on. | `/usr/local/bin/arr-media-guard` |
| Method | `POST` | |
| Username and Password | `WEBHOOK_USER` and `WEBHOOK_PASSWORD` | |
| Arguments | | empty |

An app in a container never reaches the listener at `localhost`, because that name points at the app's own container.

With `KEEP_REPLACED=true`, the app must also send **On Grab**. AMG turns it on in its own connection, at the listener's
start and in the nightly audit, and logs a line. Until then AMG keeps no files. To keep them sooner, turn on On Grab in
the connection by hand.

Turn on each app's recycle bin, on the media's file system, so a bad upgrade can be undone. In Docker, mount the bin
too, see [docs/regrabs.md](docs/regrabs.md#restore-after-a-bad-upgrade).

## Several Sonarr or Radarr instances

One install serves any number of Sonarr and Radarr instances, for example a second Sonarr for 4K. The plain keys, such
as `SONARR_URL` and `RADARR_URL`, set up the two default instances, named `sonarr` and `radarr`. `APP_INSTANCES` adds
more, as `name:program` pairs:

```
APP_INSTANCES='sonarr-4k:sonarr,radarr-4k:radarr'
SONARR_4K_URL='http://sonarr-4k:8989'
SONARR_4K_API_KEY='the key from Settings > General of that Sonarr'
RADARR_4K_URL='http://radarr-4k:7878'
RADARR_4K_API_KEY='the key from Settings > General of that Radarr'
```

- A name holds letters and digits, with a single `-` between two of them. The program is `sonarr` or `radarr`. A name
  whose keys would clash with other settings, such as `plex` or `state`, is refused.
- On a host, each name in `APP_INSTANCES` must match the Instance Name in Settings > General of its app. Sonarr accepts
  only an Instance Name that starts or ends with `Sonarr`, and Radarr only one that holds `Radarr`. So pick names such
  as `sonarr-4k` and `radarr-4k`.
- The keys of an instance start with its name in upper case, with `-` as `_`. `sonarr-4k` reads `SONARR_4K_URL`,
  `SONARR_4K_API_KEY`, `SONARR_4K_DIR`, `SONARR_4K_PATH_MAP` and `SONARR_4K_LINK`.
- The URL is required. The `_DIR` key, the app's own folder, defaults to `/var/lib/<name>`. An empty `_PATH_MAP` key
  takes `PATH_MAP`.
- A bad entry is left out, and `--selftest` names it.

**Docker.** One container serves every instance. Each instance posts to `/<name>`, so the Webhook URL of the 4K
Sonarr is `http://arr-media-guard:8484/sonarr-4k`, and of the 4K Radarr `http://arr-media-guard:8484/radarr-4k`.
The other fields are as in the table above. In this example the 4K lines are comments. Remove the `#` of each line
to add a 4K Sonarr and a 4K Radarr.

```yaml
  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    volumes: [/srv/appdata/sonarr:/config, /srv/media:/data]      # Webhook URL http://arr-media-guard:8484/sonarr
  # sonarr-4k:
  #   image: lscr.io/linuxserver/sonarr:latest
  #   volumes: [/srv/appdata/sonarr-4k:/config, /srv/media:/data]   # Webhook URL http://arr-media-guard:8484/sonarr-4k
  radarr:
    image: lscr.io/linuxserver/radarr:latest
    volumes: [/srv/appdata/radarr:/config, /srv/media:/data]      # Webhook URL http://arr-media-guard:8484/radarr
  # radarr-4k:
  #   image: lscr.io/linuxserver/radarr:latest
  #   volumes: [/srv/appdata/radarr-4k:/config, /srv/media:/data]   # Webhook URL http://arr-media-guard:8484/radarr-4k
  arr-media-guard:
    image: ghcr.io/samwiseg0/arr-media-guard:latest
    environment:
      PUID: 1000
      PGID: 1000
      TZ: Etc/UTC
      SONARR_API_KEY: CHANGE_ME              # Settings > General in Sonarr
      RADARR_API_KEY: CHANGE_ME              # Settings > General in Radarr
      # APP_INSTANCES: sonarr-4k:sonarr,radarr-4k:radarr
      # SONARR_4K_URL: http://sonarr-4k:8989
      # SONARR_4K_API_KEY: CHANGE_ME         # Settings > General in the 4K Sonarr
      # RADARR_4K_URL: http://radarr-4k:7878
      # RADARR_4K_API_KEY: CHANGE_ME         # Settings > General in the 4K Radarr
    volumes: [/srv/appdata/arr-media-guard:/config, amg-state:/config/state, /srv/media:/data]
    stop_grace_period: 1m
    restart: unless-stopped
volumes:
  amg-state: {name: amg-state}
```

**Host.** Every instance runs the same Custom Script, `/usr/local/bin/arr-media-guard`. Sonarr and Radarr pass their
Instance Name, from Settings > General, to the script. With one instance of a program, no setting is needed. With
more, set the Instance Name of each app to match its name in `APP_INSTANCES`. Case does not count, and a space counts as
`-`. So `Sonarr 4K` matches `sonarr-4k`, and the default `Sonarr` matches `sonarr`. Sonarr takes only an Instance Name
that starts or ends with `Sonarr`, and Radarr one that holds `Radarr`. The Test fails when the name matches no
instance. It also fails when another instance of the program has a name that picks the wrong instance, as a 4K Sonarr
that kept the name `Sonarr`. The worker then refuses the imports of that program, each with one error line, until
the names differ. The selftest only warns of it.

Give the user of each app read access to the env file, and write access to `STATE_DIR` and `LOG`. Schedule the
nightly audit for each instance, as `arr-media-guard --audit sonarr-4k --since 24h --post`.

**What changes per instance.** Most commands take the instance name, as `--backfill sonarr-4k`. The decision log, the
syslog line and the alerts name the instance. Each instance has its own `REGRAB_CAP` count. A file that
`KEEP_REPLACED` kept for one instance never serves another.

## Check that it works

1. Run the selftest. In Docker run `docker exec -it arr-media-guard arr-media-guard --selftest`. On a host run
   `arr-media-guard --selftest`. It also checks Plex, Discord, TMDB, SABnzbd and the indexer where they are set up. It
   warns of each one that does not answer or refuses its token or key, and changes nothing, see
   [docs/commands.md](docs/commands.md#service-checks). The listener runs the same checks at its start.
2. Press Test in each app's connection. Save runs it too. Test checks the policy, the app's API and its key, and each
   root folder of the app. A failed Test names what to fix. On a host the script answers `arr-media-guard: Test ok`, or
   exits 1 so the Test fails. In Docker, see [docs/docker.md](docs/docker.md#the-listener).
3. Run a dry run on one file. It prints the planned edit and changes nothing:
   ```
   docker exec -it arr-media-guard arr-media-guard --backfill radarr --paths "/data/movies/Film A (2000)/Film A (2000).mkv"
   ```
   On a host, leave out `docker exec -it arr-media-guard`.

To try the subtitle check on one file with no app, see [docs/subtitles.md](docs/subtitles.md#try-it-on-one-file).

## Settings

Settings come from the env file or from environment variables of the same names. The environment wins over the env file.
Every key is optional. When a value is not valid, AMG uses a safe value instead, and `--selftest` fails on it. The
worker logs it at each start. A bad `AUDIT_TIME`, `WEBHOOK_USER` or path map stops the listener instead. An env file
that is there and cannot be read fails `--selftest` and the app's Test too, and the worker logs it. A missing env file
is no error, and every key takes its default.

The env file holds one `KEY='value'` per line. A comment needs its own line, because the value runs to the end of the
line.

- In Docker the file is `arr-media-guard.env` in the folder you mount at `/config`. Restart the container after you change it.
- On a host it is `/etc/arr-media-guard.env`. Set the environment variable `ARR_MEDIA_GUARD_ENV` to use another file.
- [examples/arr-media-guard.env](examples/arr-media-guard.env) lists every key with its default.
  [docker/arr-media-guard.env](docker/arr-media-guard.env) holds the Docker values and the Docker keys.

A setting from the environment follows these rules.

- An empty variable counts as set. It empties a URL, a token, a key, `LOG`, `STATE_DIR`, `LID_DIR` and `INSTANCE`, and
  turns `AUDIT_TIME` off. An empty `WEBHOOK_USER` or `WEBHOOK_PASSWORD` stops the listener. For `NAME`, `REGRAB`,
  `SUBTITLES` and `DISCORD_POSTS` it is a bad value. Every other key takes its default.
- A value from the environment takes the same reading and the same checks as one from the file.
- Any other variable is ignored. `--selftest` names the keys it took from the environment, and never their values.
- In Docker, set them in `environment:` of the compose file, as [docker/compose.yml](docker/compose.yml) does. Run
  `docker compose up -d` after you change them. A restart keeps the old environment.
- On a host, each command reads the environment it runs in. The Custom Script reads the app's environment, and the
  nightly audit its timer's. So keep host settings in the env file.

The command `arr-media-guard` is a launcher. It runs the package `arr_media_guard/` in the folder where the launcher
really sits, after symlinks. Set the environment variable `ARR_MEDIA_GUARD_LIB` to use another folder.

### Files and names

| Key | Default | What it does |
| --- | --- | --- |
| `LOG` | `/var/log/arr-media-guard.jsonl` | The decision log, with one line for each file AMG checks, see [docs/monitoring.md](docs/monitoring.md). |
| `STATE_DIR` | `/var/lib/arr-media-guard` | Where AMG keeps its queue and records (`state.sqlite`), its locks, the lists of files that `--check-audio` and `--check-video` found, and `status.json`. Both apps must write here. |
| `POLICY_FILE` | `/etc/arr-media-guard.policy.json` | The rules for which audio and subtitles play first, see [docs/policy.md](docs/policy.md). Without it, AMG checks no import, and one alert says why. |
| `LID_DIR` | `/opt/arr-media-guard-lid` | The language detection install. Without its `ready` file, AMG hears no audio and skips the subtitle match. |
| `NAME` | `arr-media-guard` | The syslog tag, the name in alert footers, and the name of the [kept folders](docs/regrabs.md#kept-originals). Letters, digits, `.`, `_` and `-` only. |
| `INSTANCE` | the host name, in Docker `arr-media-guard` | The name of this AMG install in logs and alerts. It names no Sonarr or Radarr instance, see `APP_INSTANCES` for those. Without the key, a new host name changes it, and a new container gets a new host name. |

### Sonarr and Radarr

| Key | Default | What it does |
| --- | --- | --- |
| `RADARR_URL`, `SONARR_URL` | `http://127.0.0.1:7878`, `http://127.0.0.1:8989`, in Docker `http://radarr:7878`, `http://sonarr:8989` | The app APIs as this host or container reaches them, see [docs/commands.md](docs/commands.md#connect-to-the-apps). |
| `RADARR_DIR`, `SONARR_DIR` | `/var/lib/radarr`, `/var/lib/sonarr` | The app's own folder. With no API key set, AMG reads the key from `config.xml` there. |
| `RADARR_API_KEY`, `SONARR_API_KEY` | empty | The app's API key, from Settings > General. Set it when AMG cannot see the app's folder, as in Docker. Empty: the key in `config.xml`. |
| `RADARR_LINK`, `SONARR_LINK` | empty | The address your browser opens for the app, as `https://movies.example.com`. Each alert then links to the item there. Empty: no link. |
| `APP_INSTANCES` | empty | More instances, as `name:program` pairs joined by `,`. See [Several instances](#several-sonarr-or-radarr-instances). |
| `SONARR_4K_URL`, `SONARR_4K_API_KEY`, `SONARR_4K_DIR`, `SONARR_4K_PATH_MAP`, `SONARR_4K_LINK` | empty, `/var/lib/sonarr-4k` for the folder | The keys of the instance `sonarr-4k`. The keys of each instance start with its name in upper case, with `-` as `_`. The URL is required. |

### Plex, Discord and TMDB

| Key | Default | What it does |
| --- | --- | --- |
| `PLEX_URL`, `PLEX_TOKEN` | empty | After AMG changes a file, Plex reads it again, so Plex shows the new tracks. An empty `PLEX_URL` turns every Plex call off. |
| `DISCORD_WEBHOOK` | empty | Alerts and scan summaries go here. Empty: nothing is posted. |
| `DISCORD_POSTS` | `issues` | `issues`: a post for each problem left unresolved. `all`: also a post for each change AMG makes to a file, such as new subtitle times, a flag edit or a re-grab. Any other value, an empty one too, acts as `issues`, and `--selftest` fails on it. |
| `TMDB_TOKEN` | empty | A TMDB API read token of your own, for the language and wrong-content checks. Empty: Radarr's key, read from `/opt/Radarr/Radarr.Common.dll`, else a copy of it in the code, as in Docker. |

Each alert names the check that found the problem, such as the import check or the deep analysis, the slower subtitle
check after an import. It also names the step where AMG's fix stopped. See [docs/features.md](docs/features.md#alerts).

`arr-media-guard --test-discord` posts one test message to `DISCORD_WEBHOOK` and prints Discord's answer. It fails
when Discord refuses the message. In Docker, run `docker exec arr-media-guard arr-media-guard --test-discord`.

### Path maps

| Key | Default | What it does |
| --- | --- | --- |
| `PATH_MAP` | empty | `APP_PATH:LOCAL_PATH` pairs joined by `\|`, when AMG sees the media at other paths than the apps. |
| `SONARR_PATH_MAP`, `RADARR_PATH_MAP`, `PLEX_PATH_MAP` | empty | The map of one program. Empty: `PATH_MAP`. Each instance of `APP_INSTANCES` has its own. |

[docs/docker.md](docs/docker.md#path-maps) explains path maps with an example.

### Re-grabs and kept files

| Key | Default | What it does |
| --- | --- | --- |
| `REGRAB` | `audio,video` | Which faults delete the file, so the app searches for another copy, a *re-grab*. A fault counts only when a second check from scratch finds it again. The kinds are `audio`, `video`, `content` and `damage`, joined by commas. A kind left out only alerts. `none` turns every re-grab off. An empty value or an unknown kind fails `--selftest`, and an empty value re-grabs nothing. See [docs/regrabs.md](docs/regrabs.md#re-grabs). |
| `REGRAB_CAP` | `30` | Re-grabs per instance in 24 hours. After that, AMG only alerts. `0` turns every re-grab off. A value that is no whole number acts as `0`, and `--selftest` fails on it. |
| `RESTORE` | `true` | After a re-grab of a broken upgrade, AMG puts back the old file from the recycle bin. |
| `KEEP_REPLACED` | `false` | When the app grabs a release, AMG keeps a hard link of each file the grab may replace, so a restore works with no recycle bin. It needs `KEEP_ORIGINALS_DAYS` above 0. AMG turns on On Grab in its connection in the app, at the listener's start and in the nightly audit, and logs it. See [docs/regrabs.md](docs/regrabs.md#keep-replaced-files). |
| `KEEP_ORIGINALS_DAYS` | `7` | Days AMG keeps the old copy of each file it rewrote, a *kept original*, and each file `KEEP_REPLACED` kept. `0` keeps nothing. At `0`, AMG takes no subtitle track out of a file, and never moves or rewrites a `.srt` file, because that needs a kept copy. It still retimes a subtitle track inside the file, with no kept copy. |

With file system snapshots (ZFS, Btrfs), `KEEP_REPLACED` is optional. A broken upgrade is still re-grabbed, and the old file comes back from the app's recycle bin, or from a snapshot by hand. Leave `KEEP_ORIGINALS_DAYS` at its default. A kept original is a hard link, so on ZFS or Btrfs it shares its space with the snapshot. At `0`, a subtitle track of another episode stays in the file with its default and forced flags off, and a wrong or late `.srt` file stays as it is. A late subtitle track inside the file still gets new times.

### Repairs and conversion

| Key | Default | What it does |
| --- | --- | --- |
| `HEADER_REPAIR` | `true` | Repair a Matroska file whose header is wrong, by writing it again with the same streams. `false`: leave the header as it is. |
| `REPACK_MAX_GB` | `30` | The size limit in GB for a rewrite. AMG never rewrites a file larger than this: no header repair, no subtitle change in the file and no conversion. A value that is no number acts as `0`, so AMG rewrites no file, and `--selftest` fails on it. |
| `CONVERT` | `false` | Convert each imported file whose name does not end in `.mkv` into Matroska. A backfill converts only with `--convert`. |
| `CONVERT_MAX_FILES` | `200` | Conversions one `--convert --apply` run makes. |
| `CONVERT_WORKERS` | `1` | Conversions a `--convert --apply` run makes at a time. |

### Subtitles and workers

| Key | Default | What it does |
| --- | --- | --- |
| `SUBTITLES` | `deep` | What the subtitle check of an import does. `off`: nothing. `check`: alerts only. `fix`: also fixes. `deep`: also runs the deep analysis, a slower check after each import that listens to the whole audio, about 15 CPU minutes an hour of video. See [docs/subtitles.md](docs/subtitles.md#levels). |
| `RECHECK_ON_UPDATE` | `true` | After an update, check again each file whose saved subtitle check the new version can improve. The first start of the new version puts one recheck per file in the background queue, behind the imports. A recheck does what `SUBTITLES` allows. `false`: no recheck. See [docs/features.md](docs/features.md#recheck-after-an-update). |
| `SCAN_WORKERS` | `1` | Files a library scan or a dry-run backfill reads at a time. |
| `HOOK_WORKERS` | `2` | Jobs the worker runs at once, each in a process of its own. `1` runs each job inside the worker. A season pack still runs at most this many. |

### Subtitle hunter

These keys are for `arr-media-guard-subhunt`, see [docs/features.md](docs/features.md#subtitle-hunter).

| Key | Default | What it does |
| --- | --- | --- |
| `SABNZBD_API_KEY` | empty | The API key of Radarr's SABnzbd download client. Radarr's API hides it. Without it, the hunter stops. |
| `NEWZNAB_API_KEY` | empty | The API key of Radarr's Newznab indexer, such as NZBHydra2. Radarr's API hides it. Without it, the hunter stops. |
| `SABNZBD_URL` | empty | SABnzbd as this host or container reaches it, with its URL base, for example `http://sabnzbd:8080`. Empty: the address in Radarr's download client. Set it when that address does not resolve here. |
| `NEWZNAB_URL` | empty | The Newznab indexer as this host or container reaches it, for example `http://nzbhydra2:5076`. Empty: the address in Radarr's indexer. Set it when that address does not resolve here. |

### Docker

In Docker, the env file holds the Docker values of the keys above, as the paths under `/config`. The Docker section
at its end holds these keys.

| Key | Default in Docker | What it does |
| --- | --- | --- |
| `WEBHOOK_USER`, `WEBHOOK_PASSWORD` | `arr-admin`, empty | The user and password each app sends with its Webhook, in printable ASCII. The user takes no `:`. An empty password in the env file gets a random one at the listener's start, and an empty user there gets `arr-admin`. AMG never writes a password from the environment into the env file. When the user or the password is still empty, the listener does not start. |
| `AUDIT_TIME` | `07:30` | The local time of the nightly audit and the weekly log rotation. Empty: neither runs. A value that is not `HH:MM` stops the listener. |

## Update

1. With compose, run `docker compose pull arr-media-guard` and `docker compose up -d arr-media-guard`. The image tag
   `latest` always holds the newest release. Without compose, run these commands. `docker stop` gives a running edit
   60 seconds to finish.
   ```
   docker pull ghcr.io/samwiseg0/arr-media-guard:latest
   docker stop arr-media-guard && docker rm arr-media-guard
   ```
   Then run the `docker run` command of [Install in Docker](#install-in-docker) again. On a host, run
   `sudo git -C /opt/arr-media-guard pull`.
2. Add to your env file each new key that the release notes name. An update never changes that file. In Docker, each
   start writes the image's env file to `arr-media-guard.env.example` beside yours, so you can compare the two.
3. Run the selftest.

After an update, AMG may check some files again by itself, see `RECHECK_ON_UPDATE`.

In Docker, `/config` keeps the env file, the policy and the decision log. The volume `amg-state` keeps the state store
and the caches.

## Remove

1. Remove the connection in each app.
2. Wait until no job waits. This command prints 0 then, and the folder `queue` in `STATE_DIR` is empty. In Docker,
   run it with `docker exec` and the path `/config/state/state.sqlite`.
   ```
   python3 -c 'import sqlite3; print(sqlite3.connect("/var/lib/arr-media-guard/state.sqlite").execute("SELECT count(*) FROM jobs").fetchone()[0])'
   ```
3. In Docker, remove the service and the volume from the compose file, and run `docker compose up -d --remove-orphans`.
   Without compose, run `docker stop arr-media-guard && docker rm arr-media-guard`. Then run `docker volume rm amg-state`,
   and remove the config folder.
4. On a host, remove `/opt/arr-media-guard`, `/opt/arr-media-guard-lid`, `/usr/local/bin/arr-media-guard`,
   `/usr/local/bin/arr-media-guard-subhunt`, the two files in `/etc`, `STATE_DIR`, `LOG`, the logrotate file and the
   audit timer.
5. Remove the `.<NAME>-originals` and `.<NAME>-recycle` folders when you no longer need the kept files. They sit at
   the top of each mount, or in the highest folder AMG could write.

The track flags AMG set stay in your files. The decision log holds the undo of each edit, see
[docs/monitoring.md](docs/monitoring.md#undo-an-edit).

## More docs

| File | What it holds |
| --- | --- |
| [docs/how-it-works.md](docs/how-it-works.md) | The whole process, from an import to the alert, and the nightly job. Read it first. |
| [docs/features.md](docs/features.md) | What each check does, and when it runs. |
| [docs/policy.md](docs/policy.md) | The policy file, which picks the audio and subtitles that play first. |
| [docs/commands.md](docs/commands.md) | The backfill, dry runs, scans, conversions, the audit and Plex commands, also in Docker. |
| [docs/subtitles.md](docs/subtitles.md) | The subtitle check, `--sub-time` and its output, and a try on one file. |
| [docs/regrabs.md](docs/regrabs.md) | Re-grabs, restores after a bad upgrade, and the kept originals. |
| [docs/docker.md](docs/docker.md) | Docker detail: mounts, user and group, path maps, the listener. |
| [docs/monitoring.md](docs/monitoring.md) | The decision log, the syslog line, the undo of an edit, log rotation and `status.json`. |
| [docs/design.md](docs/design.md) | How each rule works, and why it exists. |
| [docs/development.md](docs/development.md) | The code layout and the tests. |

## License

GPL-3.0. See [LICENSE](LICENSE).
