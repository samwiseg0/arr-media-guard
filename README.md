# arr-media-guard

A Custom Script connection for Sonarr and Radarr. It runs on every import and upgrade. It makes the right audio
track play first and sets the subtitle defaults to match. It re-grabs a file whose audio is broken or whose video is
corrupt, and posts one Discord alert for each problem it finds.

Some release groups ship an English film with a Portuguese, Turkish or Hindi default track. Plex plays the default
track, so the viewer gets the wrong language. This hook fixes the flags in place with `mkvpropedit`. It never
re-encodes.

## What it does

- **Default tracks.** It classifies every audio and subtitle track once, then picks the audio and the subtitles from
  a policy file. When the signals conflict it abstains and edits nothing.
- **Language detection.** When a track's language is in doubt, an optional Whisper model hears the audio.
- **Broken audio and corrupt video.** It samples the audio that will play and decodes three short video windows.
  A certain fault deletes the file, marks the grab failed and lets the app search again. A daily cap limits this.
- **Restore after a bad upgrade.** When the broken file was an upgrade, the old file comes back from the app's
  recycle bin, if it checks clean.
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

- Linux, with Sonarr and Radarr on the same host. It is tested with Sonarr 4 and Radarr 6.
- Python 3.12 or later.
- `mkvtoolnix` and `ffmpeg` (for `mkvpropedit`, `mkvmerge`, `ffprobe` and `ffmpeg`).
  Tested with mkvtoolnix 92 and ffmpeg 7.1 (Debian 13). mkvtoolnix 82 reports no frame counts, so every header repair refuses.
- Optional: Plex, a Discord webhook, and a TMDB API read token.
- Optional: the language detection venv, about 450 MB, and its model, 464 MB. The pinned wheels are for CPython 3.13
  on x86_64.
- Known limits: the conversion reads `/var/lib/<app>/<app>.db`, and the TMDB key fallback reads `/opt/Radarr`. In a
  Docker install, set `TMDB_TOKEN` and leave `CONVERT` off for now.

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
| `KEEP_DIR` | `.arr-media-guard-originals` | The folder at the top of each mount that holds kept originals. A name without a leading dot takes the default. |
| `HIDE_DIR` | `.arr-media-guard-convert` | The folder beside the video where a remux writes its temp file, and where the original and the extras wait during a conversion. A name without a leading dot takes the default. |
| `NAME` | `arr-media-guard` | The syslog tag and the name in alert footers. |
| `INSTANCE` | the host name | The name of this host in logs and alerts. |
| `RADARR_URL`, `SONARR_URL` | `http://127.0.0.1:7878`, `http://127.0.0.1:8989` | The app APIs. |
| `RADARR_CONFIG`, `SONARR_CONFIG` | `/var/lib/radarr/config.xml`, `/var/lib/sonarr/config.xml` | The script reads the API key here. |
| `RADARR_DB` | `/var/lib/radarr/radarr.db` | The subtitle hunter reads the download client and the indexer here. |
| `PLEX_URL`, `PLEX_TOKEN` | empty | Plex re-analyzes an edited item. Plex must see the files under the same paths as the apps. An empty `PLEX_URL` turns every Plex call off. |
| `DISCORD_WEBHOOK` | empty | Alerts and scan summaries. Empty: nothing is posted. |
| `TMDB_TOKEN` | empty | A TMDB API read token. Empty: Radarr's bundled key, read from `/opt/Radarr/Radarr.Common.dll`. |
| `REGRAB_CAP` | `30` | Re-grabs per app in 24 hours for broken audio, wrong content and a damaged source. Then it alerts only. |
| `VIDEO_REGRAB_CAP` | `30` | The same for corrupt video, counted apart. |
| `WRONG_CONTENT_REGRAB` | `false` | `false`: a wrong-content file only alerts "would re-grab". |
| `DAMAGE_REGRAB` | `true` | Re-grab an import whose conversion shows a damaged source. `false`: no re-grab. A failed conversion alerts "Repack failed", and a file ffprobe cannot read is only listed. |
| `RESTORE` | `true` | A re-grab of a broken upgrade puts back the old file from the recycle bin. |
| `HEADER_REPAIR` | `true` | Remux a Matroska file whose header is wrong. `false`: log it only. |
| `KEEP_ORIGINALS_DAYS` | `7` | Days a repair keeps the file it replaced, hard-linked into `KEEP_DIR`. `0` keeps nothing. |
| `REPACK_MAX_GB` | `30` | A larger file is never remuxed or converted. |
| `CONVERT` | `false` | Convert every imported file that is not Matroska. A backfill converts only with `--convert`. |
| `CONVERT_MAX_FILES` | `200` | Conversions one `--convert --apply` run makes. |
| `CONVERT_WORKERS` | `1` | Conversions a `--convert --apply` run makes at a time. |
| `SCAN_WORKERS` | `1` | Files a library scan or a dry-run backfill reads at a time. |
| `HOOK_WORKERS` | `1` | Import jobs the hook runs at a time. A season pack still runs at most this many. |

The conversion reads the app's database beside the default `config.xml` path, `/var/lib/<app>/<app>.db`.

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
arr-media-guard --subhunt radarr --ids 123                    # find a release with English subtitles, dry. Needs SABnzbd and NZBHydra2.
arr-media-guard --audit radarr --since 24h --post             # the hook's edits of the last day, to Discord
```

An apply asks Plex to analyze each edited item, after the section is idle on two checks. When "Scan my library
automatically" is off in Plex, the analyzes that follow need one idle check each. See docs/design.md, "The burst".

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
step runs as normal. The original stays in `KEEP_DIR` for `KEEP_ORIGINALS_DAYS`, so one move puts it back. The
decision line names the refusal in `repack.forced`, and the nightly audit says the conversion was forced. The option
needs `--convert --apply` and `KEEP_ORIGINALS_DAYS` above 0. A path that is not in the run's work list is
reported and skipped.

A listed Sonarr file also skips the check that Sonarr reads its new video name as its own episodes. Scene numbering or an
alias can make that check wrong while the names are right. The conversion names the episodes by id, so the link stays
right. This needs no logged refusal. The extras beside the video keep the check, and an extra whose name maps to other
episodes still refuses the file and is named, so you can fix or remove it. The proof still runs, and a proof refusal is
forced only as above. The decision
line names the parse result in `repack.forced_name`, the original stays in `KEEP_DIR`, and the audit says "name
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

## Tests

```
python -m pytest -q
```

The tests need pytest. The tests on real media files need `ffmpeg` and `mkvtoolnix` and skip without them.

## License

GPL-3.0. See [LICENSE](LICENSE).
