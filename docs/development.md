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
| `subsync.py` | the word match, the timing fit and the speech layout of the subtitle check |
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

### Safety self-checks

Safety self-checks are checks the code runs on its own results while the tests run. `AMG_INVARIANTS=1` turns them on,
and [tests/conftest.py](../tests/conftest.py) sets it for the whole suite. Off, no check runs. A block move must keep
three safety rules, see [Subtitle block timing](subtitles.md#subtitle-block-timing).

- Own evidence. A cue that a block moves has evidence of its own, by the rules of `subsync.evidence()`. Its anchor sits
  at the block's offset, or its heard words lie in the block. After a long silence, or with its anchor over 0.3 seconds
  off the block, its words alone do not move it. It also needs its own onset where the block puts it. Evidence that puts
  it on the line keeps it where it is, and cuts the block there. A cue in a block's `keep`, or outside every block,
  moves only by the whole-track fix.
- Order. The order of cue starts never changes, and two starts that did not tie never tie. Two cues clamped to 0
  seconds may tie, because no start lies before 0. A moved cue may still start before the end of a cue that stays.
- Nearer its speech. A moved cue never ends up farther from its speech than it was. A cue with its own onset where it
  sat stays. Its anchor breaks the rule only when it ends farther from two lines, each by more than 0.01 seconds. They
  are the line the move uses and the part's line. The line the move uses is the line beside the block, or the track's
  lean. So the real margin is 0.01 seconds plus the distance between the two lines. That is up to about 0.15 seconds
  beside a block, and 0.3 seconds against the lean. The rule catches an overshoot, and the harnesses measure a wrong
  move against the planted truth.

`subsync.blocks()` checks the first and the last rule, and `remux.time_plan()` checks the order. Two other paths move
cues, and each has its own checks.

- Live caption timing. `subsync.check_live()` checks the first and the last rule of each cue that `live_moves()`
  moves. Its own evidence is its own anchor, or a span between two anchors. A cue moved by its anchor ends nearer it.
  A cue moved between anchors ends nearer every point of the span when the span proved the move. It ends inside the
  span when the anchors around it moved the same way.
- Incorrect subtitle identification. `subsync.nearer()` checks that a fix of `layout_fix()` never lowers the share of
  the speech that the lines show over.

A broken rule raises `subsync.Broken`, which names the rule and the cue. An import job, a deep analysis job and a
`--sub-time` file pass it up to the caller. When `AMG_INVARIANT_DUMP` names a folder, the case goes there first as
JSON. It holds the inputs of the check, and for a block the arguments that call `blocks()` again.

## Build the image

[docker.md](docker.md#build-the-image-yourself) has the build command.
