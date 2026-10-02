# Docker

The image holds both commands, `ffmpeg`, `mkvtoolnix`, the language detection venv and the pinned Whisper model. The build
checks the model's sha256, and nothing downloads at run time. Sonarr and Radarr reach it through a **Webhook**
connection, so nothing goes into their containers. The published image is for amd64. An arm64 image is not published
yet.

The README has the install steps. This file has the detail.

## A full compose file

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
    image: ghcr.io/samwiseg0/arr-media-guard:latest
    container_name: arr-media-guard
    environment:
      PUID: 1000
      PGID: 1000
      TZ: Etc/UTC                  # your time zone, for AUDIT_TIME and the log times
      RADARR_API_KEY: CHANGE_ME    # Settings > General in Radarr
      SONARR_API_KEY: CHANGE_ME    # Settings > General in Sonarr
    volumes:
      - ./arr-media-guard:/config
      - amg-state:/config/state
      - /srv/media:/data
      # - /dev/log:/dev/log   # optional, for Loki
    stop_grace_period: 1m
    restart: unless-stopped
volumes:
  amg-state:
    name: amg-state   # the same name in a docker run, so a one-off container shares the store
```

This compose file publishes no port of arr-media-guard. Only the apps on the compose network reach it, at
`http://arr-media-guard:8484`. [docker/compose.yml](../docker/compose.yml) publishes port 8484, for apps on another
network or host. [Mounts](#mounts) says what each mount holds.

## Apps on other hosts

The image can run on its own Docker host while Sonarr and Radarr run elsewhere. It reads the apps only through their
APIs, and no feature reads an app's database, so every feature works there. Change the compose file and the env file
this way:

1. Set `SONARR_URL` and `RADARR_URL` to URLs this container can reach.
2. Publish the listener's port, for example `ports: ["8484:8484"]`. In each app, the Webhook URL is then
   `http://<docker host>:8484/sonarr` or `http://<docker host>:8484/radarr`. An instance of `APP_INSTANCES` posts to
   `/<name>`, see the README.
3. Mount the media, and each app's recycle bin, at the apps' paths, or set [path maps](#path-maps).
4. For the subtitle hunter, mount SABnzbd's download folder too, at SABnzbd's path or in a `RADARR_PATH_MAP` pair.
   Set `SABNZBD_URL` and `NEWZNAB_URL` when the addresses Radarr has saved do not resolve from this container.

Every Docker install sets `SONARR_API_KEY` and `RADARR_API_KEY`, in the environment or the env file, as
[docker/compose.yml](../docker/compose.yml) does. See the README.

## Mounts

| Mount | What it holds |
| --- | --- |
| `/config` | The env file, `policy.json` and the decision log in `logs/`. A folder you can edit. |
| `/config/state` | The state store `state.sqlite`, the caches, the locks and `status.json`, in the named volume `amg-state`. The store is SQLite in WAL mode and needs a local disk. A one-off container mounts the same volume, so it takes the same file lock. |
| The media | At the same path as in the apps, `/data` above. Else set a path map, see [Path maps](#path-maps). |
| Sonarr's download folder | Optional, read-only. The episode title check reads the scene NFO of each import there, through Sonarr's path map. Without it, the release NFO that Sonarr copied beside the video gives the title, when Sonarr imports `nfo` extras. Else the release name gives it. |
| SABnzbd's download folder | For the subtitle hunter only, read-write. The hunter checks each download there and deletes it, through Radarr's path map. |
| `/dev/log` | Optional, for Loki. Each command in the container then also writes its logfmt lines to the host's syslog, as on a host. Without it, only the lines of the listener, its worker and its nightly jobs reach a log, the container log. |

## The env file in Docker

The first start writes `arr-media-guard.env` and `policy.json` into `/config`. It writes the env file only on its first
start, so add by hand the keys a new release names. Each start writes the image's env file as
`arr-media-guard.env.example`, so the keys of a new release show there.

`WEBHOOK_USER` and `WEBHOOK_PASSWORD` are the user and password of the Webhook connection. The Webhook connection of
each app takes the same pair as its Username and Password. Each takes any printable ASCII, and the user takes no `:`.
The env file starts with the user `arr-admin` and an empty password. When the password is empty, the listener generates
a random one at its start and writes it into the env file. The container log then names the file, and never shows the
password. Without a user, the listener does not start. A `WEBHOOK_PASSWORD` or `WEBHOOK_USER` from the environment never
goes into the env file, see [Settings in the environment](#settings-in-the-environment).

The env file holds the Docker values in place of the host values, as the paths under `/config`. The Docker section at
its end holds the Docker keys in the README's settings. Every other key works as on a host. The listener reads the env
file and the policy when it starts. Restart the container after you change either.

## Settings in the environment

Each setting may also come from an environment variable of the same name, and the environment wins over the env file.
The README's [Settings](../README.md#settings) has the rules. This compose file keeps the plain settings in
`environment:` and the keys and tokens in a file of their own, which compose loads as environment variables too:

```yaml
services:
  arr-media-guard:
    image: ghcr.io/samwiseg0/arr-media-guard:latest
    container_name: arr-media-guard
    environment:
      PUID: 1000
      PGID: 1000
      TZ: Europe/Berlin
      RADARR_URL: http://radarr:7878
      SONARR_URL: http://sonarr:8989
      REGRAB: audio,video,content
    env_file: ./secrets.env   # the keys and tokens, mode 0600
    volumes: [./arr-media-guard:/config, amg-state:/config/state, /srv/media:/data]
    stop_grace_period: 1m
    restart: unless-stopped
volumes:
  amg-state: {name: amg-state}
```

```
# secrets.env, one KEY=value per line
RADARR_API_KEY=CHANGE_ME
SONARR_API_KEY=CHANGE_ME
```

- A `WEBHOOK_PASSWORD` from the environment is never generated and never written. An empty one stops the listener. So
  set a password there, or leave the key out and let the listener generate one into the env file.
- A `WEBHOOK_USER` from the environment never goes into the env file either.
- A key the environment does not set keeps its value in `/config/arr-media-guard.env`. An install that sets every key
  in that file works as before.
- Run `docker compose up -d` after a change. A restart keeps the old environment.
- `docker exec arr-media-guard arr-media-guard --selftest` names the keys it took from the environment.

## User and group

Each command runs as `PUID:PGID`, so the files it writes keep the owner of the media. Set `PUID` and `PGID` to the user
and group the apps run as.

The start script, [docker/arr-media-guard.sh](../docker/arr-media-guard.sh), runs the command with
`setpriv --clear-groups`, which drops every other group. So a media folder that only a supplementary group of that user
may write stays read-only for the hook.

`PUID:PGID` must be able to write the kept folders at the top of each mount.
[regrabs.md](regrabs.md#where-the-folders-go) says where they go when it cannot.

## Time zone

Set `TZ` on the container to your time zone, for example `TZ=Europe/Berlin`. Without it the container runs in UTC, so
`AUDIT_TIME` and the times in the decision log are UTC.

## Path maps

Sonarr, Radarr, Plex and this container can each see the media under other paths. A path map pairs the paths of one
program with the paths this script sees. Path maps work on a host install too.

| Key | The map of |
| --- | --- |
| `SONARR_PATH_MAP` | Sonarr. |
| `RADARR_PATH_MAP` | Radarr. |
| `PLEX_PATH_MAP` | Plex. |
| `PATH_MAP` | Every program whose own map is empty. |

- Each pair is `PROGRAM_PATH:LOCAL_PATH`, both absolute, for example `/data:/media`. `|` joins the pairs. A path may
  hold spaces.
- A map that is empty takes `PATH_MAP`, so one map serves every program that sees the same paths.
- A pair matches whole folder names only, so `/mnt/TV` never matches `/mnt/TV Shows`.
- When two pairs match, the longer one wins.
- A pair that is not two absolute paths fails `--selftest`, and the listener does not start.

`--selftest` and the listener start check the maps against each app's root folders. A root folder this script does not
see gives a warning, and so does one that no Plex library folder holds. Each warning names the folder and the setting
to fix.

### Example

The host keeps the three libraries in `/srv/media`, and each program mounts them at its own paths.

| Library | Sonarr | Radarr | Plex | This container |
| --- | --- | --- | --- | --- |
| TV | `/mnt/TV` | | `/mnt/TV Shows` | `/media/TV` |
| Anime | `/mnt/Anime` | | `/mnt/Anime` | `/media/Anime` |
| Movies | | `/movies` | `/mnt/Movies` | `/media/Movies` |

```yaml
services:
  sonarr:
    volumes: [/srv/media/TV:/mnt/TV, /srv/media/Anime:/mnt/Anime]
  radarr:
    volumes: [/srv/media/Movies:/movies]
  plex:
    volumes: ["/srv/media/TV:/mnt/TV Shows", /srv/media/Anime:/mnt/Anime, /srv/media/Movies:/mnt/Movies]
  arr-media-guard:
    volumes: [./arr-media-guard:/config, amg-state:/config/state, /srv/media:/media]   # one media mount for TV, Anime and Movies
volumes:
  amg-state: {name: amg-state}
```

```
SONARR_PATH_MAP='/mnt/TV:/media/TV|/mnt/Anime:/media/Anime'
RADARR_PATH_MAP='/movies:/media/Movies'
PLEX_PATH_MAP='/mnt/TV Shows:/media/TV|/mnt/Anime:/media/Anime|/mnt/Movies:/media/Movies'
```

This container mounts `/srv/media` once. A rename never crosses two mounts, even on one file system.

Give each app a recycle bin in a folder it mounts, such as `/mnt/TV/.recycle` in Sonarr and `/movies/.recycle` in
Radarr. Both sit in `/srv/media`, so the maps above reach them, and a restore can rename a file back from the bin. With
`KEEP_REPLACED=true` the hook keeps its own copies, and the app bins are optional.
[regrabs.md](regrabs.md#in-the-apps-and-plex) says how the apps and Plex show these folders.

## The listener

The listener takes the Webhook posts of the apps. The worker runs in the same container, and the listener starts it
again when a job waits.

- It takes each post only with the right user and password, and a body of at most 1 MiB.
- It asks the app for the file by its id and uses the path the API gives.
- It refuses a file of another item, and an old file outside the item's folder or the recycle bin, with a line in the
  decision log.
- It queues an import even when the app's API fails or this container does not see the file, with one warning line.
  The worker asks the app again, after a minute at first and at most an hour apart, for a day.
- Each request has 10 seconds in all, and 32 run at once, so a slow or idle client never holds up an app.
- 64 more connections may wait for a thread. The listener closes each new one past those 96 at once.
- A post with a wrong path or wrong credentials never reaches the decision log. The listener counts them and prints one
  summary line a minute at most.
- Once a day at `AUDIT_TIME` it runs the nightly audit of each app. The audit also removes kept originals and the grab
  links of `KEEP_REPLACED` older than `KEEP_ORIGINALS_DAYS`. At the same time the listener runs the weekly log
  rotation. Empty: neither runs.
- It answers the Test of each app. Test checks the policy, the API key and each root folder of the app. A failed Test
  names what to fix. When `KEEP_REPLACED` keeps no copies, Test warns in the log when the app's recycle bin is off,
  elsewhere or not mounted.
- At its start it prints a banner, then runs the same checks for each app with an API key and prints one line per app.
  It asks an app that does not answer again for 2 minutes. A failed check warns, and the listener keeps running.
- The container log also carries the logfmt summary of each decision line and of each nightly audit, see
  [monitoring.md](monitoring.md#syslog).
- A stop waits for a running flag edit.
- The healthcheck asks `http://127.0.0.1:8484/health`. A healthcheck that passes writes no line to the container log.

[design.md](design.md#webhook) has the rules behind the listener.

## Move from a host install

1. Remove the Custom Script connection in each app, and add the Webhook.
2. Copy the keys you changed from `/etc/arr-media-guard.env` into the Docker env file or the environment. Keep the Docker
   file's paths.
3. Mount the media at the paths the apps use, so the container sees each file at the path the apps give.

## Build the image yourself

In a clone, run:

```
docker build -f docker/Dockerfile -t arr-media-guard .
```
