# arr-media-guard

arr-media-guard checks every file that Sonarr or Radarr imports or upgrades. It makes the right audio track play
first and sets the subtitle defaults to match. It re-grabs a file that is certainly broken, and posts one Discord alert
for each problem it finds. It edits the track flags in place with `mkvpropedit`, and never re-encodes.

- **Default tracks.** It picks the audio and the subtitles that play first, from a policy file.
- **Language detection.** An optional Whisper model hears a track whose language is in doubt.
- **Subtitle match.** It finds a subtitle of another episode, and retimes a subtitle that runs late.
- **Broken files.** It re-grabs a file with broken audio or corrupt video, and can restore the old file.
- **Wrong content.** It alerts when a file holds another film or another episode.
- **Repairs.** It repairs a wrong Matroska header, and can convert AVI, MP4, M4V, TS and WebM files into Matroska.
- **Backfill and scans.** It fixes and scans your whole library, and audits its own edits.
- **Subtitle hunter.** A command of its own replaces a Radarr film that has no English subtitle with a release that
  has one.

[docs/features.md](docs/features.md) says more about each check.

## Requirements

- Docker. The image holds Python, the tools, language detection and its model. It is for amd64. An arm64 image is not
  published yet.
- Sonarr 4 and Radarr 6. These are the versions it is tested with.
- Optional: Plex, a Discord webhook, and a TMDB API read token.

On a host, it needs these instead of Docker:

- Linux and Python 3.12 or later, with Sonarr and Radarr on the same host.
- `mkvtoolnix` and `ffmpeg`, for `mkvpropedit`, `mkvmerge`, `ffprobe` and `ffmpeg`. It is tested with mkvtoolnix 92
  and ffmpeg 7.1 (Debian 13). mkvtoolnix 82 reports no frame counts, so every header repair refuses.
- Optional: the language detection venv, about 450 MB, and its model, 464 MB. The pinned wheels are for CPython 3.13
  on x86_64 and aarch64.

## Install in Docker

Docker is the recommended way. The image works wherever Sonarr and Radarr run. It needs each app's API at a URL it
can reach, and the media at the apps' paths or through [path maps](docs/docker.md#path-maps). The apps reach it
through a **Webhook** connection, so nothing goes into their containers.

These steps run it next to the apps, on one compose network. For apps on other hosts, see
[docs/docker.md](docs/docker.md#apps-on-other-hosts). No feature reads an app's database, so every feature works there too.

1. Add the service and its volume to the compose file of your apps:
   ```yaml
   services:
     arr-media-guard:
       image: ghcr.io/samwiseg0/arr-media-guard:2.0.0
       container_name: arr-media-guard
       environment: [PUID=1000, PGID=1000, TZ=Etc/UTC]   # the apps' user and group, and your time zone
       volumes:
         - ./arr-media-guard:/config   # the env file, the policy and the decision log
         - amg-state:/config/state     # the state store
         - /srv/media:/data            # the media, at the same path as in the apps
         # - /dev/log:/dev/log         # optional, for Loki
       stop_grace_period: 1m
       restart: unless-stopped
   volumes:
     amg-state:
       name: amg-state
   ```
   The state store is SQLite in WAL mode and needs a local disk, so it gets a volume of its own. To ship syslog to
   Loki, also mount `/dev/log`. Each command in the container then writes its logfmt lines to the host's syslog.
2. Run `docker compose up -d`. The first start writes `arr-media-guard.env` and `policy.json` into `/config`.
   `WEBHOOK_USER` and `WEBHOOK_PASSWORD` in the env file are the user and password of the Webhook connection. The user
   is `arr-admin`. The listener generates a random password at its first start and writes it into the env file. You can
   set your own pair instead. Each takes any printable ASCII, and the user takes no `:`.
3. Open `./arr-media-guard/arr-media-guard.env`. Copy `WEBHOOK_USER` and `WEBHOOK_PASSWORD` into the Username and
   Password of each app's Webhook connection, see [Add it to Sonarr and Radarr](#add-it-to-sonarr-and-radarr). Every app
   takes the same pair. Set `SONARR_API_KEY` and `RADARR_API_KEY` to the keys in each app's Settings > General. Set
   `TMDB_TOKEN` too, because the image has no Radarr install to read the bundled key from.
4. Run `docker compose restart arr-media-guard`, so the listener reads the keys.

[docs/docker.md](docs/docker.md) has a full compose file, the mounts, path maps for other media paths, and the
listener.

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
3. Give the app users write access. Sonarr and Radarr run the script as their own user. That user must be able to
   write `STATE_DIR`, `LOG` and your media files. Both apps share the state folder and its locks. So give them a shared
   group, or run both as the same user. [docs/regrabs.md](docs/regrabs.md#where-the-folders-go) says where the hook
   keeps the files it replaces.
4. Add `OOMPolicy=continue` to the `[Service]` section of both app units. The hook's children run in the app's
   cgroup, and this stops an OOM kill of a child from restarting the app. See [Memory](docs/design.md#memory).
5. Rotate the decision log weekly with logrotate, and leave out `copytruncate`.
   [docs/monitoring.md](docs/monitoring.md#rotate-the-decision-log) has a ready file.
6. Schedule `arr-media-guard --audit radarr --since 24h --post` nightly, and the same for `sonarr`. Use a systemd
   timer or cron. The audit reviews the hook's own edits, and removes old kept originals and grab links.
7. Run `arr-media-guard --selftest`.

To turn on language detection, also run:

```
sudo python3 -m venv /opt/arr-media-guard-lid/venv
sudo /opt/arr-media-guard-lid/venv/bin/pip install --require-hashes --only-binary=:all: \
    -r /opt/arr-media-guard/arr_lid.requirements.txt
sudo /opt/arr-media-guard-lid/venv/bin/python /opt/arr-media-guard/arr_media_guard/lid.py --fetch \
    --model-dir /opt/arr-media-guard-lid/models
sudo touch /opt/arr-media-guard-lid/ready
```

`--fetch` downloads the pinned model once and checks its sha256. The hook uses detection only when `ready` exists.
Remove `ready` before you change the venv, and create it again after.

## Add it to Sonarr and Radarr

In each app, open Settings, Connect, and add a connection. Use a **Webhook** in Docker and a **Custom Script** on a
host.

| Field | Webhook (Docker) | Custom Script (host) |
| --- | --- | --- |
| Name | `arr-media-guard` | `arr-media-guard` |
| Triggers | **On File Import** and **On File Upgrade** | **On File Import** and **On File Upgrade** |
| URL or Path | `http://arr-media-guard:8484/sonarr` in Sonarr, `http://arr-media-guard:8484/radarr` in Radarr | `/usr/local/bin/arr-media-guard` |
| Method | `POST` | |
| Username and Password | `WEBHOOK_USER` and `WEBHOOK_PASSWORD` | |
| Arguments | | empty |

With `KEEP_REPLACED=true`, also turn on **On Grab**.

Turn on each app's recycle bin, on the media's file system, so a bad upgrade can be undone. In Docker, mount the bin
too, see [docs/regrabs.md](docs/regrabs.md#restore-after-a-bad-upgrade).

## Several Sonarr or Radarr instances

One install serves any number of Sonarr and Radarr instances, for example a second Sonarr for 4K. The keys of the
settings below set up the instances `sonarr` and `radarr`. `APP_INSTANCES` adds more, as `name:program` pairs:

```
APP_INSTANCES='sonarr-4k:sonarr,radarr-4k:radarr'
SONARR_4K_URL='http://sonarr-4k:8989'
SONARR_4K_API_KEY='the key from Settings > General of that Sonarr'
RADARR_4K_URL='http://radarr-4k:7878'
RADARR_4K_API_KEY='the key from Settings > General of that Radarr'
```

- A name holds letters and digits, with a single `-` between two of them. The program is `sonarr` or `radarr`.
- On a host, a Sonarr instance name starts or ends with `sonarr`, and a Radarr instance name holds `radarr`. The
  Instance Name of each app must match its instance name, and the apps refuse other names.
- The keys of an instance start with its name in upper case, with `-` as `_`. `sonarr-4k` reads `SONARR_4K_URL`,
  `SONARR_4K_API_KEY`, `SONARR_4K_DIR` and `SONARR_4K_PATH_MAP`.
- The URL is required. The folder defaults to `/var/lib/<name>`. An empty map takes `PATH_MAP`.
- A bad entry is left out, and `--selftest` names it.

**Docker.** One container serves every instance. Each instance posts to `/<name>`, so the Webhook URL of the 4K
Sonarr is `http://arr-media-guard:8484/sonarr-4k`. The other fields are as in the table above. For example:

```yaml
  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    volumes: [./sonarr:/config, /srv/media:/data]      # Webhook URL http://arr-media-guard:8484/sonarr
  sonarr-4k:
    image: lscr.io/linuxserver/sonarr:latest
    volumes: [./sonarr-4k:/config, /srv/media:/data]   # Webhook URL http://arr-media-guard:8484/sonarr-4k
  arr-media-guard:
    image: ghcr.io/samwiseg0/arr-media-guard:2.0.0
    environment: [PUID=1000, PGID=1000, TZ=Etc/UTC]
    volumes: [./arr-media-guard:/config, amg-state:/config/state, /srv/media:/data]
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
the names differ. The selftest and the Docker checks only warn of it.

Give the user of each app write access to `STATE_DIR` and `LOG`. Schedule the nightly audit for each instance, as
`arr-media-guard --audit sonarr-4k --since 24h --post`.

**What changes per instance.** Every command takes the instance name, as `--backfill sonarr-4k`. The decision log, the
syslog line and the alerts name the instance. Each instance has its own `REGRAB_CAP` count. A grab link that one
instance kept never serves another.

## Check that it works

1. Run the selftest. In Docker run `docker exec -it arr-media-guard arr-media-guard --selftest`. On a host run
   `arr-media-guard --selftest`.
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

The env file holds one `KEY='value'` per line. A comment needs its own line, because the value runs to the end of the
line. Every key is optional. A value the script cannot read takes the safest reading, and `--selftest` fails on it.
The worker logs it at each start.

- In Docker the file is `./arr-media-guard/arr-media-guard.env`. Restart the container after you change it.
- On a host it is `/etc/arr-media-guard.env`. Set the environment variable `ARR_MEDIA_GUARD_ENV` to use another file.
- [examples/arr-media-guard.env](examples/arr-media-guard.env) lists every key with its default.
  [docker/arr-media-guard.env](docker/arr-media-guard.env) holds the Docker values and the Docker keys.

The command `arr-media-guard` is a launcher. It runs the package `arr_media_guard/` in its own folder, symlinks
resolved. Set the environment variable `ARR_MEDIA_GUARD_LIB` to use another folder.

### Files and names

| Key | Default | What it does |
| --- | --- | --- |
| `LOG` | `/var/log/arr-media-guard.jsonl` | The decision log, see [docs/monitoring.md](docs/monitoring.md). |
| `STATE_DIR` | `/var/lib/arr-media-guard` | The state store `state.sqlite`, the locks, the scan lists and `status.json`. |
| `POLICY_FILE` | `/etc/arr-media-guard.policy.json` | The decision policy, see [docs/policy.md](docs/policy.md). |
| `LID_DIR` | `/opt/arr-media-guard-lid` | The language detection venv and model. |
| `NAME` | `arr-media-guard` | The syslog tag, the name in alert footers, and the name of the [kept folders](docs/regrabs.md#kept-originals). Letters, digits, `.`, `_` and `-` only. |
| `INSTANCE` | the host name, in Docker `arr-media-guard` | The name of this install in logs and alerts. A container gets a new host name with each new container. |

### Sonarr and Radarr

| Key | Default | What it does |
| --- | --- | --- |
| `RADARR_URL`, `SONARR_URL` | `http://127.0.0.1:7878`, `http://127.0.0.1:8989`, in Docker `http://radarr:7878`, `http://sonarr:8989` | The app APIs as this host or container reaches them, see [docs/commands.md](docs/commands.md#connect-to-the-apps). |
| `RADARR_DIR`, `SONARR_DIR` | `/var/lib/radarr`, `/var/lib/sonarr` | The app's own folder, which holds `config.xml`. |
| `RADARR_API_KEY`, `SONARR_API_KEY` | empty | The API key of an app whose folder this host does not see, as in Docker. Empty: the key in `config.xml`. |
| `APP_INSTANCES` | empty | More instances, as `name:program` pairs joined by `,`. See [Several instances](#several-sonarr-or-radarr-instances). |
| `SONARR_4K_URL`, `SONARR_4K_API_KEY`, `SONARR_4K_DIR`, `SONARR_4K_PATH_MAP` | empty, `/var/lib/sonarr-4k` for the folder | The keys of the instance `sonarr-4k`. The keys of each instance start with its name in upper case, with `-` as `_`. The URL is required. |

### Plex, Discord and TMDB

| Key | Default | What it does |
| --- | --- | --- |
| `PLEX_URL`, `PLEX_TOKEN` | empty | Plex re-analyzes an edited item. An empty `PLEX_URL` turns every Plex call off. |
| `DISCORD_WEBHOOK` | empty | Alerts and scan summaries go here. Empty: nothing is posted. |
| `TMDB_TOKEN` | empty | A TMDB API read token. Empty: Radarr's bundled key, read from `/opt/Radarr/Radarr.Common.dll`. |

### Path maps

| Key | Default | What it does |
| --- | --- | --- |
| `PATH_MAP` | empty | `APP_PATH:LOCAL_PATH` pairs joined by `\|`, when this script sees the media at other paths than the apps. |
| `SONARR_PATH_MAP`, `RADARR_PATH_MAP`, `PLEX_PATH_MAP` | empty | The map of one program. Empty: `PATH_MAP`. Each instance of `APP_INSTANCES` has its own. |

[docs/docker.md](docs/docker.md#path-maps) explains path maps with an example.

### Re-grabs and kept files

| Key | Default | What it does |
| --- | --- | --- |
| `REGRAB` | `audio,video` | The kinds of fault that re-grab: `audio`, `video`, `content` and `damage`. See [docs/regrabs.md](docs/regrabs.md#re-grabs). |
| `REGRAB_CAP` | `30` | Re-grabs per instance in 24 hours, then it alerts only. `0` turns every re-grab off. A value that is no whole number acts as `0`, and `--selftest` fails on it. |
| `RESTORE` | `true` | A re-grab of a broken upgrade puts back the old file from the recycle bin. |
| `KEEP_REPLACED` | `false` | At each grab, keep a hard link of each file the grab may replace, for a restore. See [docs/regrabs.md](docs/regrabs.md#keep-replaced-files). |
| `KEEP_ORIGINALS_DAYS` | `7` | Days the hook keeps a file it replaced, and each grab link. `0` keeps nothing. |

### Repairs and conversion

| Key | Default | What it does |
| --- | --- | --- |
| `HEADER_REPAIR` | `true` | Remux a Matroska file whose header is wrong. `false`: log it only. |
| `REPACK_MAX_GB` | `30` | A larger file is never remuxed or converted. A value that is no number acts as `0`, so no file is remuxed, and `--selftest` fails on it. |
| `CONVERT` | `false` | Convert every imported file that is not Matroska. A backfill converts only with `--convert`. |
| `CONVERT_MAX_FILES` | `200` | Conversions one `--convert --apply` run makes. |
| `CONVERT_WORKERS` | `1` | Conversions a `--convert --apply` run makes at a time. |

### Subtitles and workers

| Key | Default | What it does |
| --- | --- | --- |
| `SUBTITLES` | `fix` | What the subtitle check of an import does: `off`, `check`, `fix` or `deep`. See [docs/subtitles.md](docs/subtitles.md#levels). |
| `SCAN_WORKERS` | `1` | Files a library scan or a dry-run backfill reads at a time. |
| `HOOK_WORKERS` | `1` | Import jobs the hook runs at a time. A season pack still runs at most this many. |

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
| `WEBHOOK_USER`, `WEBHOOK_PASSWORD` | `arr-admin`, empty | The user and password of the Webhook connection of each app, in printable ASCII. The user takes no `:`. An empty password gets a random one at the listener's start, in the env file. Without a user, the listener does not start. |
| `AUDIT_TIME` | `07:30` | The local time of the nightly audit and the weekly log rotation. Empty: neither runs. |

## Update

1. In Docker, set the new version in `image:`, then run `docker compose pull arr-media-guard` and
   `docker compose up -d arr-media-guard`. On a host, run `sudo git -C /opt/arr-media-guard pull`.
2. Add the keys a new release names to your env file. An update never changes that file. In Docker, each start writes
   the image's env file to `arr-media-guard.env.example` beside yours, so you can compare the two.
3. Run the selftest.

In Docker, `/config` keeps the env file, the policy and the decision log. The volume `amg-state` keeps the state store
and the caches.

## Remove

1. Remove the connection in each app.
2. Wait until no job waits. This command prints 0 then. In Docker, run it with `docker exec` and the path
   `/config/state/state.sqlite`.
   ```
   python3 -c 'import sqlite3; print(sqlite3.connect("/var/lib/arr-media-guard/state.sqlite").execute("SELECT count(*) FROM jobs").fetchone()[0])'
   ```
3. In Docker, remove the service and the volume from the compose file, and run `docker compose up -d --remove-orphans`.
   Then run `docker volume rm amg-state`, and remove the `./arr-media-guard` folder.
4. On a host, remove `/opt/arr-media-guard`, `/opt/arr-media-guard-lid`, `/usr/local/bin/arr-media-guard`,
   `/usr/local/bin/arr-media-guard-subhunt`, the two files in `/etc`, `STATE_DIR`, `LOG`, the logrotate file and the
   audit timer.
5. Remove the `.<NAME>-originals` and `.<NAME>-recycle` folders at the top of each mount when you no longer need the
   kept files.

The track flags the hook set stay in your files. The decision log holds the undo of each edit, see
[docs/monitoring.md](docs/monitoring.md#undo-an-edit).

## More docs

| File | What it holds |
| --- | --- |
| [docs/how-it-works.md](docs/how-it-works.md) | The whole process, from an import to the alert, and the nightly job. Read it first. |
| [docs/features.md](docs/features.md) | What each check does. |
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
