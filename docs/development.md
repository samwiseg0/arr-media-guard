# Development

[how-it-works.md](how-it-works.md) follows an import through the code, step by step. Read it first.

## Code layout

`arr-media-guard` and `arr-media-guard-subhunt` are launchers. Each one puts its own folder on `sys.path` and runs the
code. The package `arr_media_guard/` holds the code of `arr-media-guard`. `arr_subhunt.py` holds the subtitle hunter.
The hunter imports the package, and the package never imports the hunter.

| Module | What it holds |
| --- | --- |
| `cli.py` | `main()`, the backfill, `--sub-time`, the audit and the scans |
| `runner.py` | `hook()`, the Custom Script entry, the `Event` it and the listener read, the Test checks, the queue, the worker, the job processes and the file lock |
| `process.py` | `process()`, the checks and the flag edit of one file |
| `config.py` | the env file as `CFG`, the policy load, the tuning constants and `mask()` |
| `apps.py` | Sonarr and Radarr behind one interface, `ARR[app]`, the HTTP client and the path maps |
| `checks.py` | the probes, the header check, broken audio, corrupt video and the spoken language |
| `subtitles.py` | the subtitle match check |
| `remux.py` | the header repair, the subtitle remux, and the swap they share with the conversion |
| `convert.py`, `proof.py` | the conversion into Matroska, and its packet proof |
| `regrab.py` | the re-grab, and the restore of the old file of an upgrade |
| `vault.py` | the kept files and their prune |
| `plex.py` | the Plex analyze and the folder scans |
| `logs.py` | the decision log, the syslog line and the Discord posts |
| `report.py` | the words of every decision output: the decision line, the syslog summary, the alerts and the CLI line |
| `serve.py` | the Webhook listener for Docker |
| `decide.py` | the flag decision rules |
| `content.py` | the metadata checks, and `DEADLINE`, the job's time limit |
| `subsync.py` | the word match and the timing fit of the subtitle check |
| `lid.py` | language detection. The venv in `LID_DIR` runs it by its path. |
| `health.py` | `status.json` for a monitoring agent |
| `store.py` | the state store, one SQLite file in `STATE_DIR` |

A module calls a function of another module through that module, as `apps.arr(...)`, and reads the settings as
`config.CFG`. It never imports a name from another module. So a test that patches a name in its module reaches every
caller.

## Tests

Run the tests from the top of a clone:

```
python -m pytest -q
```

The tests need pytest. The tests on real media files need `ffmpeg` and `mkvtoolnix`, and skip without them.

The tests assert codes and fields. [tests/test_report.py](../tests/test_report.py) holds the golden text of each
template in `report.py`, so a reworded template changes that file only.

The tests reach the package through [tests/amg.py](../tests/amg.py). `amg.load()` gives each test file its own copy of
the package, with its own settings. Its `hook.name` reads and writes `name` in the module that defines it, so
`monkeypatch.setattr(hook, "arr", fake)` replaces `apps.arr` for every caller.

## Build the image

[docker.md](docker.md#build-the-image-yourself) has the build command.
