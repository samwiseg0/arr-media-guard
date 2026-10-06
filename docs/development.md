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
| `subtitles.py` | the subtitle match check, and the saved results with their registry, `SUB_CHECKS` |
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
With pytest-xdist installed, `python -m pytest -q -n auto` runs them on every core, as CI does.

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
  span when the anchors around it moved the same way. `remux.time_plan()` checks the ends with `subsync.live_ends()`.
  A cue of the block, or the cue just before it, never ends past its next later new start, the first new start after
  its own. A cue that ran back to back with the next one ends there. So the move opens no gap and adds no overlap.
  Cues on one new start, as cues clamped to 0, may show together until the next later new start.
- Foreign subtitle timing. `subsync.nearer()` checks that a fix of `layout_fix()` never lowers the share of the
  speech that the lines show over. `subsync.check_kept()` checks a partial shift: the order of the lines, kept runs
  only at the file's ends, and the core's edge, the first and last moved line with a speech start at the fix, where
  the stay votes have fallen over KEEP_SLACK under their peak. Every other moved line beyond it must be one the edge's
  line passes by KEEP_PASS.

A broken rule raises `subsync.Broken`, which names the rule and the cue. An import job, a deep analysis job and a
`--sub-time` file pass it up to the caller. When `AMG_INVARIANT_DUMP` names a folder, the case goes there first as
JSON. It holds the inputs of the check, and for a block the arguments that call `blocks()` again.

## When a change can fix old files

A release that improves a subtitle check can fix files that an older version already checked. AMG keeps a *saved
result* for each file it checks, so the next `--sub-check` skips that file. Without a rule, an old result would keep
the file from the fix for good. One small registry in the code sets that rule per check. Edit it in every release
that changes a subtitle check.

### The saved result

After each subtitle check, AMG saves one result per file in `lid.sqlite`, the `subcheck` table, by path, size and mtime.
`subtitles.sub_cache()` writes it. It has these fields.

- `checks` holds each check the run made, with its `version` and its `found`, the findings of that check.
- `app` is the instance that listed the file, or none for a file no app lists.
- `mode` is the run that made it. That is `import`, `sub_check` for `--sub-check`, `sub_time` for `--sub-time`, or
  `deep` for the deep analysis.
- `sidecars` holds the size and mtime of each `.srt` file beside the video.

A file that changes gets no saved result until its next check. A result saved by 2.4.0 or older has no `checks`. It
never counts, so the next `--sub-check` checks that file again, once.

### The checks and their findings

`SUB_RUNS` in `subtitles.py` names the checks of each run. `--sub-check` skips a file only when its result comes from a
run that makes every check of `--sub-check`. An import makes fewer checks, so its result does not count.

| Check | Feature | Runs that make it |
| --- | --- | --- |
| `subtitle_match` | Subtitle match, the word check, and its fix by one shift or a frame rate | all |
| `reference_timing` | Subtitle match, the timing of a subtitle against one whose words matched | all |
| `flash` | Subtitle match, the lines that flash by too fast to read | all |
| `foreign_timing` | Foreign subtitle timing and Incorrect subtitle identification | `sub_check`, `sub_time`, `deep` |
| `garbled_repair` | Garbled subtitle repair | `sub_check`, `sub_time`, `deep` |
| `block_timing` | Subtitle block timing and Live caption timing | `sub_time`, `deep` |

`subtitles.sub_found()` turns the result of each subtitle into findings. A check's `found` lists each finding once.
An empty list means a clean check. A check that could not judge, as Foreign subtitle timing after a failed speech
read, says `unread` and never gives an empty list.

| Finding | Meaning |
| --- | --- |
| `mismatch` | The subtitle does not belong to the audio. |
| `unknown` | The check gave no verdict. |
| `fix` | The check found new times or a repair. A dry run or `SUBTITLES=check` may not have made it. |
| `off` | The times are off by one amount, and they stay. The alert says the subtitles seem late or early. |
| `steps` | The times are off by different amounts in different parts of the file. |
| `unfixable` | Garbled text that AMG cannot repair. |
| `cut` | The read of the track stopped part way. |
| `live` | Live captions. |
| `unread` | The check could not read what it judges, as when the speech read failed or timed out. The result stays pending, so the next `--sub-check` tries again. |

### The registry

`SUB_CHECKS` in `subtitles.py` holds each check's `version` and its `fixes`:

```python
SUB_CHECKS = {
    "foreign_timing": {"version": 1, "fixes": {}},
    ...
}
```

`fixes` maps a version to the old results that version can fix. A list names findings of those results. It names a
result when any one of its words is among that check's findings in the result. So `["off", "steps"]` names a result
with `off`, one with `steps`, and one with both. `None` names every old result. A result is *stale* when a `fixes`
entry above its version, up to the current one, names it.

An entry counts only for the results of runs that make the check, see `SUB_RUNS`. An import makes `subtitle_match`,
`reference_timing` and `flash`. So an entry on one of those three makes the saved results of imports stale too, and
each of those files gets a recheck after the update. Such a recheck makes only the import's checks, see below. An
entry on `foreign_timing`, `garbled_repair` or `block_timing` never reaches an import's result.

`test_each_registry_entry_names_real_findings_and_versions` fails when an entry names a word that is not in
`subtitles.SUB_FINDINGS`, or a version above the check's `version`. Run the tests after each edit.

For each check you change in a release:

1. Raise its `version`, for every change.
2. Decide whether the change can fix files an older version checked.
   - It cannot, as for a change of speed, wording or the log. Add nothing to `fixes`, and every old result stays.
   - It can fix some of them. Add `fixes[new version]` with the findings it fixes.
   - It can fix any of them. Add `fixes[new version] = None`.
3. Keep the old entries. A result two versions old is stale when any entry since its version names it.
4. Add a test that a result with the named findings is stale and one without them is not, as
   `test_a_new_version_makes_stale_only_the_results_it_names` does.

An example. Version 2 of Foreign subtitle timing fixes the subtitles that version 1 left late or early. Version 3
only runs faster.

```python
"foreign_timing": {"version": 3, "fixes": {2: ["off"]}},
```

A result of version 1 with `off` is stale. A result of version 1 without `off`, and every result of version 2, stays.

A real case. HandBrake ends each ASS event with a NUL byte, and the versions before could not write new times into
such a track. Version 2 of the flash check and version 3 of Subtitle block timing can. A result with `fix` does not
say whether the fix was written, so each one gets a recheck. A recheck of a fix that was written finds nothing to move.

```python
"flash": {"version": 2, "fixes": {2: ["fix"]}},
"block_timing": {"version": 3, "fixes": {3: ["fix"]}},
```

A second real case. Subtitle match 2 reads a roll-up caption by its new line, and the deep analysis fits a drift from
its sweep. Both can fix or clear times that version 1 left off or in steps. Block timing 4 reads a straight drift as a
drift, never as live captions. So a result with `live` is stale, and so is one whose sweep alerted.

```python
"subtitle_match": {"version": 2, "fixes": {2: ["off", "steps"]}},
"block_timing": {"version": 4, "fixes": {3: ["fix"], 4: ["live", "off"]}},
```

Only results that flagged timing get a recheck. A result with no timing finding stays, even when the new version would
now find a drift there. A recheck of an import's or a `--sub-check` result also runs at that depth. It hears no sweep,
so it cannot reach the drift fix. Such a file needs `arr-media-guard --sub-time <file> --apply`.

A new check goes into `SUB_CHECKS` and into `SUB_RUNS`. A result saved before it lacks the check, and reads as version
0 with no findings. So `fixes = {1: None}` runs the new check on every file checked before. Without that entry those
files stay as they are until they change.

### What a stale result does

- The next `--sub-check` checks the file again. `--sub-check --recheck` checks a file whatever its result says.
- With `RECHECK_ON_UPDATE=true`, the default, the first start of a new version queues a recheck of each stale file,
  see `runner.queue_rechecks()`. On a host, the first nightly audit of the version does it. The version is the hash
  of the code, `config.VERSION`. The state store marks it as done once every app answered. After a stop part way, the
  next start queues the rest, and a file whose job waits or runs gets no second one.
- A recheck is a job of the *background queue*, the jobs in the state store. Imports always go first, then the deep
  analyses, then the rechecks. A deep analysis or a recheck runs only while no import waits, one at a time.
- A recheck repeats the checks of the run that saved the result, and never checks deeper. A result of an import gets
  the import's subtitle checks only, with no read of the whole file, no Foreign subtitle timing and no Garbled
  subtitle repair. A result of `--sub-check`, `--sub-time` or the deep analysis gets that run's checks.
- `SUBTITLES` decides what a recheck may do, as for an import. `off` queues nothing, and a recheck queued before
  `SUBTITLES` went off drops itself before it reads the file. `check` only alerts, and `fix` and `deep` fix. A fix that
  `check` or a dry run leaves unmade keeps the saved result pending, so a later `--sub-check --apply` makes it. A
  recheck keeps the flags, and alerts only on subtitles, as the deep analysis does. Its decision lines have `source`
  `recheck`.
- The recheck takes the item from the app when it is queued, as `--sub-check` does. A file no app lists gets no
  recheck.

## Build the image

[docker.md](docker.md#build-the-image-yourself) has the build command.
