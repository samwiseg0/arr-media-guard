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
| `align.py` | the whole-file timing of `--sub-time` and the deep analysis: the anchors, the offset curve and the moves |
| `judge.py` | the timing outcome of each subtitle, after the moves and the write, and the record the alerts read |
| `lid.py` | language detection. The venv in `LID_DIR` runs it by its path. |
| `burnin.py` | the burned-in subtitle check, `quick()` and `full()`. The venv in `LID_DIR` runs it by its path, as `lid.py`. |
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
The media tests of the burned-in subtitle check in `tests/test_burnin.py` also need numpy, onnxruntime, PyAV and the
text models, and skip without them.
With pytest-xdist installed, `python -m pytest -q -n auto` runs them on every core, as CI does.

The tests assert codes and fields. [tests/test_report.py](../tests/test_report.py) holds the golden text of each
template in `report.py`, so a reworded template changes that file only.

The tests reach the package through [tests/amg.py](../tests/amg.py). `amg.load()` gives each test file its own copy of
the package, with its own settings. Its `hook.name` reads and writes `name` in the module that defines it, so
`monkeypatch.setattr(hook, "arr", fake)` replaces `apps.arr` for every caller.

### Safety self-checks

Safety self-checks are checks the code runs on its own results while the tests run. `AMG_INVARIANTS=1` turns them on,
and [tests/conftest.py](../tests/conftest.py) sets it for the whole suite. Off, no check runs. Each path that moves
subtitle lines has its own rules.

- Whole-file timing, see [Whole-file timing](subtitles.md#whole-file-timing). `align.check_plan()` checks every move
  of `align.run()`. The quoted names are entries of `subsync.TIMING`.
  - Order. No line crosses another. Two line starts stay "shown" apart, or as far apart as they were.
  - Start. No line starts before the file's start.
  - Stacked. A line never shows past the start of the line after it. The line after is the first later line that it
    did not show over (`align.after()`). Lines shown inside a long line do not count. A line that ran up to the next
    one within "touch" counts as not showing over it. The rule checks a pair only when the line's end or the start of
    the line after changed. Lines keep their order, so no line then shows over any later line it did not show over.
  - Speech. An anchored line moves toward its own speech and never past it.
  - Shown. A moved line shows "shown" at least, unless it showed less before or the line after starts sooner.
  - Ends. A line keeps its length, unless the line after makes it change. It grows to "shown" at most. When it ran up
    to the line after and that is the next line in start order, it may grow to "end hold" or its old length.
- The order of a time plan. `remux.time_plan()` checks the new starts with `subsync.ordered()`. The order of cue starts
  never changes, and two starts that did not tie never tie. Two cues that the plan clamps to 0 seconds may tie, because
  no start lies before 0.
- The burned-in subtitle mute. `decide.invariants()` keeps the only English subtitles on under audio in another
  language. A burn-in job is the one exception. When two passes read English text burned into the picture, it turns
  the English subtitles off, see [Burned-in subtitles](design.md#burned-in-subtitles). It marks the file first.
  `process.mute_rule()` checks every flag edit. An edit that turns off the only English subtitles under such audio needs
  the mark, and an edit of a marked file never turns an English subtitle on.
- Foreign subtitle timing. `subsync.nearer()` checks that a fix of `layout_fix()` never lowers the share of the
  speech that the lines show over. `subsync.check_kept()` checks a partial shift. It checks the order of the lines,
  kept runs only at the file's ends, and the core's edge. The edge is the first and last moved line with a speech
  start at the fix. There the stay votes have fallen over KEEP_SLACK under their peak. Every other moved line beyond it
  must be one the edge's line passes by KEEP_PASS.

The timing outcome has its own rules, see `judge.check_outcome()` and [Timing outcome](design.md#timing-outcome). Each
outcome is a sound record. A subtitle that posts nothing gets no timing sentence, and one whose lines are still off
gets one. A sentence on lines that flash by too fast to read says
nothing of the times, so it counts for neither rule. No sentence of a subtitle whose lines moved says they were left
as they are. The judge reads the evidence, the plan and its write. Of an earlier stage it reads only the measures that
came with the evidence, such as a reference fit's slices, so no flag outlives its facts.

A broken rule raises `subsync.Broken`, which names the rule and the cue. An import job, a deep analysis job and a
`--sub-time` file pass it up to the caller. Hook mode passes it up too, though it swallows every other error. A job
process sends it to the worker through its pipe, and the worker raises it again, see `runner.finished()`. When
`AMG_INVARIANT_DUMP` names a folder, the case goes there first as JSON. It holds the inputs of the check. For the
whole-file timing that is the lines, the anchors and the moves.

## How to add a check

A check of the subtitle times adds evidence and proposals. It never decides whether a subtitle posts, and it never
rewrites another check's keys. The timing judge, `judge.outcomes()`, decides that once, after the moves and the write.

1. Hear or read what the check needs, and keep it in the decision record. That can be a heard window with its
   offset, or a slice of a reference. The whole-file timing keeps its record of each subtitle in `rec["whole"]`, see
   `subtitles.sub_whole()`.
2. Propose a move when the check finds one, as a fix of the whole track, or new starts for `remux.time_plan()`. A gate
   in `process.subtitle_checks()` decides which proposal the plan takes.
3. Teach the judge to read the evidence. `judge.outcomes()` has one branch for each kind of subtitle. A subtitle the
   whole-file timing timed goes to `judge.whole_outcome()`, and one only the word check read to
   `judge.window_outcome()`. A subtitle timed by a reference or by the speech layout has its own branch. Add the
   evidence to each branch it can reach.
4. Read the evidence where the plan puts the lines, as `align.judged()` fits the curve again at the new starts. Give
   each stretch still off its first and last line, and count its lines by their cue starts. Mark a stretch that holds
   the first or the last line, as `judge.whole_stretches()` does. The alert words the place from these facts.
5. Name the clock in `judge.CLOCKS`. Only one clock may decide a subtitle. The whole-file timing decides every
   subtitle it judged, and the word check's windows decide only where it did not run. Nothing ranks a clock by itself.
6. Teach `judge.heard_again()` the new evidence. The import holds a timing alert for the deep analysis, and the deep
   analysis drops it only when it heard each held place again. A place that only the new check heard keeps the alert.
7. Read every threshold from `subsync.TIMING`. A new meaning gets a new entry there, with its value once.
8. Add the check's shapes to the regression corpus, `tests/test_regressions.py`, and a test to `tests/test_judge.py`.
   `judge.check_outcome()` must pass on them. A case the check fixes leaves `tests/fixtures/known_failures.json` in the
   same commit.
9. Register the check in `subtitles.py`, see below. That is its `SUB_CHECKS` entry, the runs that make it in `SUB_RUNS`,
   and its findings in `subtitles.sub_found()`. `sub_cache()` saves the findings of each check of `SUB_RUNS`, so a check
   that `sub_found()` does not list raises a KeyError there. `sub_found()` files a subtitle still off under the check
   that matched or timed it. It files it under `block_timing` too only when a stretch of the whole-file timing put it
   off. Name where a new clock's stretches go.

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

The burned-in subtitle check has no entry. It keeps no saved result, so no update rechecks old files for it. A release
that wants to check old files for burned-in subtitles needs a queue of its own for that, see "Burned-in subtitles" in
design.md.

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

A second real case. Subtitle match 2 reads a roll-up caption by its new line, and the deep analysis of 2.7.0 fitted a
drift. Both can fix or clear times that version 1 left off or in steps. Block timing 4 reads a straight drift as a
drift, never as live captions. So a result with `live` is stale, and so is one whose timing alerted.

```python
"subtitle_match": {"version": 2, "fixes": {2: ["off", "steps"]}},
"block_timing": {"version": 4, "fixes": {3: ["fix"], 4: ["live", "off"]}},
```

A third real case. Block timing 5 hears the whole file and times every line. It can fix a file that any older
version checked, so every older result of `--sub-time` and the deep analysis gets one recheck. A result does not say
whether its file has a text subtitle in the audio's language. So a file with none gets its recheck too, and no
whole-file hearing runs for it. Subtitle match 3 gives its fix's place to the whole-file timing in those runs. Alone,
at an import or `--sub-check`, it fixes no more than version 2, so it adds no entry.

```python
"subtitle_match": {"version": 3, "fixes": {2: ["off", "steps"]}},
"block_timing": {"version": 5, "fixes": {3: ["fix"], 4: ["live", "off"], 5: None}},
```

A recheck of an import's or a `--sub-check` result runs at that depth. It never hears the whole file, so it cannot
reach the whole-file timing. Such a file needs `arr-media-guard --sub-time <file> --apply`.

A new check goes into `SUB_CHECKS` and into `SUB_RUNS`. A result saved before it lacks the check, and reads as version
0 with no findings. So `fixes = {1: None}` runs the new check on every file checked before. Without that entry those
files stay as they are until they change.

### What a stale result does

- The next `--sub-check` checks the file again. `--sub-check --recheck` checks a file whatever its result says.
- With `RECHECK_ON_UPDATE=true`, the default, the first start of a new version queues a recheck of each stale file,
  see `runner.queue_rechecks()`. On a host, the first nightly audit of the version does it. The version is the hash
  of the code, `config.VERSION`. The state store marks it as done once every app answered. After a stop part way, the
  next start queues the rest, and a file whose job waits or runs gets no second one.
- A recheck is a job of the *background queue*, the jobs in the state store. Imports always go first, then the
  burned-in subtitle checks, then the deep analyses, then the rechecks. Every job but an import runs only while no
  import waits, one at a time.
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
