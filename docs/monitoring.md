# Logs and monitoring

arr-media-guard (AMG) writes a line for each file it checks to the *decision log*, and a one-line summary of it to
syslog. The file `status.json` tells a monitoring tool whether the policy file loaded and whether the TMDB key works.

## The decision log

The decision log is the file `LOG` names: `/var/log/arr-media-guard.jsonl` by default, and in Docker
`/config/logs/arr-media-guard.jsonl`. It holds one JSON line, the *decision line*, for each file AMG checks. That covers
imports, the deep analysis, the backfill and the other commands, the scans and the subtitle hunter. The nightly audit
writes a line only for a file it has to check again. A change gets lines of its own too. Examples are the undo line
before a flag edit and the line after a remux.

A decision line holds:

- the app and the ids of the item
- every track, with its language, how sure AMG is of it, and its role, such as main or commentary
- the planned edits and the reason codes, short codes that say why
- an `outcome` code, and `result`, the same in a sentence
- `findings`, the problems AMG found, each with its kind, its facts and what AMG did. `alert_kinds` lists the kinds
  alone.
- `alerts`, the text of each problem
- `alert_result`, what the Discord post of each problem gave. That is `sent`, `already sent`, `log only` for a problem
  AMG fixed, or why the post failed.
- `change_result`, with `DISCORD_POSTS=all` only, what the post of each change to the file gave. That is `sent`, why the
  post failed, or `skipped` while Discord asked AMG to wait. A change whose text could not be built posted nothing, and
  its entry starts `no text:`. A run that changed nothing has an empty list.

Search the codes, never the sentences, because the words of a sentence may change. [design.md](design.md#alerts) lists
the kinds and the codes. A dry run words its alerts as what `--apply` would do. [design.md](design.md#logs) has more on
the fields.

## Syslog

Each decision line also goes to syslog as one summary line of `key=value` pairs, tagged with `NAME`. In Docker the line
also goes to the container log. Loki reads these keys, so they stay as they are.

| Key | Value |
| --- | --- |
| `arr` | the app |
| `source` | what made the line: `hook` for an import, `backfill` for the backfill and the other commands, `deep_analysis`, `audit`, `audio_scan`, `video_scan` or `subhunt` |
| `outcome` | the outcome code |
| `class` | the kind of plan, as a short code |
| `edits` | the number of flag edits |
| `reasons` | the reason codes, comma-separated |
| `alerts` | the alert kinds, comma-separated |
| `tmdb` | the TMDB code, or `not_asked` |
| `label` | the item, else the file name, else the job name |
| `id` | the id of the decision line |
| `job` | the name of the job in the background queue, the same name the `queued ... as <job>` line of the container log shows. Empty for a line no job made, as in a backfill. |
| `error` | the result of an `error` line, cut to 150 characters. Other lines have no `error` key. |

The nightly audit writes one more syslog line a day, with `source=audit outcome=summary` and its counts.

## Undo an edit

Before each flag edit, AMG writes an `editing` line. Its `undo` field is the full `mkvpropedit` command that sets the
old flags and tags back. To reverse the last edit of a film, run this.

```
python3 - <<'PY'
import json, subprocess
recs = [json.loads(line) for line in open("/var/log/arr-media-guard.jsonl")]
last = [r for r in recs if r.get("result") == "editing" and r["label"] == "Film A (2000)"][-1]
subprocess.run(last["undo"], check=True)
PY
```

In Docker, run the same script in the container, with the log at its Docker path.

```
docker exec -i arr-media-guard python3 - <<'PY'
import json, subprocess
recs = [json.loads(line) for line in open("/config/logs/arr-media-guard.jsonl")]
last = [r for r in recs if r.get("result") == "editing" and r["label"] == "Film A (2000)"][-1]
subprocess.run(last["undo"], check=True)
PY
```

A remux, a moved or rewritten `.srt` file, and some conversions keep the old file instead.
[regrabs.md](regrabs.md#undo-a-change) says how to put it back.

## Rotate the decision log

The decision log grows with each file. On a host, rotate it weekly with logrotate. Leave out `copytruncate`, because AMG
opens the log again for each line it writes.

The Docker image rotates with these lines. On a host, put them in `/etc/logrotate.d/arr-media-guard`.

```
/var/log/arr-media-guard.jsonl {
    weekly
    rotate 52
    compress
    delaycompress
    missingok
    notifempty
}
```

In Docker the listener runs this rotation each day at `AUDIT_TIME`, and it rotates the log once a week, see
[docker.md](docker.md#the-listener).

## Status file

`STATE_DIR/status.json` is for a monitoring tool, such as Zabbix. It says whether the policy file loaded and whether the
TMDB key works. It also holds when each one last changed, and the counts of the last day. `last_hook_run` is the last
time an import, a command or the audit recorded a check. So an old value shows that AMG stopped.
[design.md](design.md#status-file) has the fields.
