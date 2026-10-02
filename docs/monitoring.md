# Logs and monitoring

The hook writes one line per file to the decision log, and a summary of it to syslog. `status.json` holds the state of
its checks for a monitoring agent.

## The decision log

`LOG` gets one JSON line per file that a run looks at. That covers the hook, every backfill, the scans and the audit.
The default is `/var/log/arr-media-guard.jsonl`, and in Docker `/config/logs/arr-media-guard.jsonl`.

A line holds:

- the app and item ids
- every track with its language, confidence and role
- the plan and the reason codes
- an `outcome` code, and `result`, the same in a sentence
- `findings`, each with its kind, its facts and the action the hook took, and `alert_kinds`, the kinds alone
- `alerts`, the text of each finding

Query the codes, never the sentences. The kinds and the action codes are listed in
[design.md](design.md#alerts), and the words of a sentence may change. A dry run words its alerts as what `--apply`
would do. [design.md](design.md#logs) has more on the fields and the codes.

## Syslog

The same summary goes to syslog as one logfmt line, tagged with `NAME`. In Docker the line also goes to the container
log. Loki reads these keys, so they stay.

| Key | Value |
| --- | --- |
| `arr` | the app |
| `source` | `hook`, `backfill`, `deep_analysis`, `audit`, `audio_scan`, `video_scan` or `subhunt` |
| `outcome` | the outcome code |
| `class` | the class of the plan |
| `edits` | the number of flag edits |
| `reasons` | the reason codes, comma-separated |
| `alerts` | the alert kinds, comma-separated |
| `tmdb` | the TMDB code, or `not_asked` |
| `label` | the item |
| `id` | the id of the decision line |

The nightly audit writes one more line a day, with `source=audit outcome=summary` and its counts.

## Undo an edit

Before each edit, the hook writes an `editing` line. Its `undo` field is the full `mkvpropedit` command that restores
the old flags. To reverse the last edit of a film:

```
python3 - <<'PY'
import json, subprocess
recs = [json.loads(line) for line in open("/var/log/arr-media-guard.jsonl")]
last = [r for r in recs if r.get("result") == "editing" and r["label"] == "Film A (2000)"][-1]
subprocess.run(last["undo"], check=True)
PY
```

In Docker, run the same script in the container, with the log at its Docker path:

```
docker exec -i arr-media-guard python3 - <<'PY'
import json, subprocess
recs = [json.loads(line) for line in open("/config/logs/arr-media-guard.jsonl")]
last = [r for r in recs if r.get("result") == "editing" and r["label"] == "Film A (2000)"][-1]
subprocess.run(last["undo"], check=True)
PY
```

A remux, a sidecar move and a conversion keep the old file instead. [regrabs.md](regrabs.md#undo-a-change) says how to
put it back.

## Rotate the decision log

The decision log grows by one line per file. On a host, rotate it weekly with logrotate.

- Leave out `copytruncate`, because the script opens the log for each line.

The Docker image rotates with these lines. On a host, put them in `/etc/logrotate.d/arr-media-guard`:

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

In Docker the listener runs this rotation at `AUDIT_TIME`, see [docker.md](docker.md#the-listener).

## Status file

`STATE_DIR/status.json` records the TMDB and policy checks for a monitoring agent.
[design.md](design.md#status-file) has the fields.
